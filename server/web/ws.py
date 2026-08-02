"""State/decode/waterfall WebSocket streams (§10.2, AD-011, §11.3).

Three separate bounded delivery paths so no client can stall another
stream (SC8): state coalesces to the newest authoritative snapshot and
closes an irrecoverably slow client; decodes stay ordered on a bounded
queue with replayable history for reconnects; waterfall reuses the lossy
drop-oldest ``SpectrumFanout``.  Every upgrade authenticates by secure
cookie and validates Host/Origin — no query token (§10.2).  A controller
state-stream disconnect drops the lease and invokes the dead-man STOP via
``LeaseService.disconnect`` (§15.4).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..engine.waterfall import SpectrumFanout
from .api import COOKIE_NAME, AppState, _snapshot
from .auth import host_allowed, origin_allowed

STATE_QUEUE = 4            # coalesced snapshots in flight per client
STATE_CLOSE_AFTER_DROPS = 16  # irrecoverably slow: never drains
DECODE_QUEUE = 64          # ordered batches in flight per client
DECODE_HISTORY = 32        # batches replayed on reconnect


# ---- framework-free broadcasters (unit-tested without sockets) -------------


class StateSubscription:
    """One state client: newest-wins queue, closed when hopelessly behind."""

    def __init__(self, max_frames: int, close_after_drops: int) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(max_frames)
        self.consecutive_drops = 0
        self._close_after = close_after_drops
        self.closed = False

    def _offer(self, snapshot: dict[str, Any]) -> None:
        if self.closed:
            return
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()  # preserve only the latest state
            self.consecutive_drops += 1
            if self.consecutive_drops >= self._close_after:
                self.closed = True  # irrecoverably slow client (§10.2)
                return
        self.queue.put_nowait(snapshot)

    async def get(self) -> dict[str, Any] | None:
        item = await self.queue.get()
        self.consecutive_drops = 0
        return item

    def close(self) -> None:
        self.closed = True


class StateBroadcaster:
    """Revisioned snapshot fan-out; only the latest state matters."""

    def __init__(self, *, max_queue: int = STATE_QUEUE, close_after_drops: int = STATE_CLOSE_AFTER_DROPS) -> None:
        self._max = max_queue
        self._close_after = close_after_drops
        self._subscribers: list[StateSubscription] = []
        self.published = 0

    def subscribe(self) -> StateSubscription:
        self._subscribers = [s for s in self._subscribers if not s.closed]
        sub = StateSubscription(self._max, self._close_after)
        self._subscribers.append(sub)
        return sub

    def publish(self, snapshot: dict[str, Any]) -> None:
        self.published += 1
        for sub in self._subscribers:
            sub._offer(snapshot)


class DecodeSubscription:
    """One decode client: strictly ordered; overflow closes the client."""

    def __init__(self, max_frames: int) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(max_frames)
        self.closed = False

    def _offer(self, batch: dict[str, Any]) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(batch)
        except asyncio.QueueFull:
            self.closed = True  # ordered stream must never drop (§10.2)
            return False
        return True

    def close(self) -> None:
        self.closed = True


class DecodeBroadcaster:
    """Ordered bounded decode fan-out with reconnect history (§10.2)."""

    def __init__(self, *, max_queue: int = DECODE_QUEUE, history: int = DECODE_HISTORY) -> None:
        self._max = max_queue
        self._history = history
        self._subscribers: list[DecodeSubscription] = []
        self.batches: list[dict[str, Any]] = []  # replay ring

    def subscribe(self, *, replay: bool = True) -> DecodeSubscription:
        self._subscribers = [s for s in self._subscribers if not s.closed]
        sub = DecodeSubscription(self._max)
        if replay:
            for batch in self.batches[-self._history :]:
                if not sub._offer(batch):
                    break
        self._subscribers.append(sub)
        return sub

    def publish(self, batch: dict[str, Any]) -> None:
        self.batches.append(batch)
        del self.batches[: max(0, len(self.batches) - self._history)]
        for sub in self._subscribers:
            sub._offer(batch)


# ---- WebSocket glue -----------------------------------------------------------


def _authenticate_upgrade(state: AppState, websocket: WebSocket) -> str | None:
    """Cookie session plus Host/Origin validation; returns the session id."""

    host = websocket.headers.get("host")
    if not host_allowed(host, state.allowed_hosts):
        return None
    if not origin_allowed(websocket.headers.get("origin"), host, state.allowed_hosts):
        return None
    session = state.auth.authenticate(websocket.cookies.get(COOKIE_NAME))
    return None if session is None else session.id


async def _reject(websocket: WebSocket, code: int, reason: str) -> None:
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


async def _pump(
    websocket: WebSocket,
    subscription: Any,
    get: Any,
    send: Any,
) -> None:
    """Forward queued items until the subscription or the socket dies.

    ``websocket.receive`` runs alongside every queue wait so a client
    disconnect is noticed immediately instead of after the next publish.
    """

    while not subscription.closed:
        get_task = asyncio.ensure_future(get())
        recv_task = asyncio.ensure_future(websocket.receive())
        done, pending = await asyncio.wait(
            {get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if recv_task in done:
            recv_task.result()  # raises WebSocketDisconnect on client close
            continue  # ignore unexpected client traffic
        await send(get_task.result())


def _with_lease_view(
    snapshot: dict[str, Any], state: AppState, session_id: str
) -> dict[str, Any]:
    """Decorate a broadcast snapshot with this connection's lease view.

    Broadcast snapshots are built once with ``session=None``, so
    ``lease.mine`` is always false.  Each state-stream send rewrites only
    that one flag from the connection's own session — a shallow copy, not
    a per-subscriber snapshot rebuild (§10.2 fan-out cost).
    """

    lease = snapshot.get("lease")
    if not isinstance(lease, dict):
        return snapshot
    current = state.lease.current()
    mine = current is not None and current.session_id == session_id
    if lease.get("mine") == mine:
        return snapshot
    return {**snapshot, "lease": {**lease, "mine": mine}}


def create_ws_router(state: AppState) -> APIRouter:
    """Build the three ``/ws/v1`` endpoints bound to one composition root."""

    router = APIRouter()

    @router.websocket("/ws/v1/state")
    async def state_stream(websocket: WebSocket) -> None:
        session_id = _authenticate_upgrade(state, websocket)
        if session_id is None:
            await _reject(websocket, 4401, "unauthenticated")
            return
        if state.state_broadcast is None:
            await _reject(websocket, 4503, "state stream unavailable")
            return
        await websocket.accept()
        subscription: StateSubscription = state.state_broadcast.subscribe()
        try:
            hello = _with_lease_view(_snapshot(state, None), state, session_id)
            await websocket.send_json({"type": "snapshot", **hello})
            await _pump(
                websocket,
                subscription,
                subscription.get,
                lambda snapshot: websocket.send_json(
                    {"type": "state", **_with_lease_view(snapshot, state, session_id)}
                ),
            )
        except WebSocketDisconnect:
            pass
        finally:
            subscription.close()
            # Controller gone: drop the lease; dead-man STOP if TX is queued
            # or active (§15.4).  Non-holders are ignored by the service.
            state.lease.disconnect(session_id)

    @router.websocket("/ws/v1/decodes")
    async def decode_stream(websocket: WebSocket) -> None:
        session_id = _authenticate_upgrade(state, websocket)
        if session_id is None:
            await _reject(websocket, 4401, "unauthenticated")
            return
        if state.decode_broadcast is None:
            await _reject(websocket, 4503, "decode stream unavailable")
            return
        await websocket.accept()
        subscription: DecodeSubscription = state.decode_broadcast.subscribe()
        try:
            await _pump(
                websocket,
                subscription,
                subscription.queue.get,
                lambda batch: websocket.send_json({"type": "decodes", **batch}),
            )
        except WebSocketDisconnect:
            pass
        finally:
            subscription.close()

    @router.websocket("/ws/v1/waterfall")
    async def waterfall_stream(websocket: WebSocket) -> None:
        session_id = _authenticate_upgrade(state, websocket)
        if session_id is None:
            await _reject(websocket, 4401, "unauthenticated")
            return
        fanout: SpectrumFanout | None = state.waterfall_fanout
        if fanout is None:
            await _reject(websocket, 4503, "waterfall stream unavailable")
            return
        await websocket.accept()
        subscription = fanout.subscribe()
        try:
            await _pump(
                websocket,
                subscription,
                subscription.queue.get,
                lambda frame: websocket.send_bytes(frame.to_bytes()),
            )
        except WebSocketDisconnect:
            pass
        finally:
            subscription.close()

    return router
