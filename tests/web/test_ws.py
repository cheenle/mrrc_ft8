"""WebSocket stream regressions: bounded backpressure per §10.2 (SC8)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_audio_tx import FakeOutputStream

from server.engine.audio_tx import TxPlayer
from server.engine.repository import Repository
from server.engine.safety import SafetyController
from server.engine.sequencer import Sequencer
from server.engine.waterfall import SpectrumFanout, SpectrumFrame
from server.web.api import AppState, create_app
from server.web.auth import AuthService, hash_password
from server.web.lease import LeaseService
from server.web.ws import DecodeBroadcaster, StateBroadcaster

PASSWORD = "correct horse battery staple"


# ---- broadcaster units --------------------------------------------------------


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_state_coalesces_to_newest_and_closes_slow_clients() -> None:
    broadcaster = StateBroadcaster(max_queue=2, close_after_drops=3)
    sub = broadcaster.subscribe()
    for revision in range(5):
        broadcaster.publish({"revision": revision})
    # revisions 0-2 were dropped; the third consecutive drop closes the client
    kept = [sub.queue.get_nowait()["revision"] for _ in range(sub.queue.qsize())]
    assert kept == [3]
    assert sub.consecutive_drops == 3
    assert sub.closed
    broadcaster.publish({"revision": 5})
    assert sub.queue.qsize() == 0  # closed subscription receives nothing


def test_state_get_resets_the_drop_counter() -> None:
    async def main() -> None:
        broadcaster = StateBroadcaster(max_queue=1, close_after_drops=3)
        sub = broadcaster.subscribe()
        broadcaster.publish({"revision": 1})
        assert await sub.get() == {"revision": 1}
        broadcaster.publish({"revision": 2})
        assert await sub.get() == {"revision": 2}
        assert not sub.closed

    run(main())


def test_decode_stream_is_ordered_and_overflow_closes() -> None:
    broadcaster = DecodeBroadcaster(max_queue=2, history=8)
    sub = broadcaster.subscribe(replay=False)
    for seq in range(3):
        broadcaster.publish({"slot_id": seq, "messages": []})
    assert sub.closed  # third batch overflowed the bounded queue
    assert [sub.queue.get_nowait()["slot_id"] for _ in range(2)] == [0, 1]  # ordered, no drops


def test_decode_history_replays_on_reconnect() -> None:
    broadcaster = DecodeBroadcaster(max_queue=64, history=3)
    for seq in range(5):
        broadcaster.publish({"slot_id": seq, "messages": []})
    sub = broadcaster.subscribe()
    replayed = [sub.queue.get_nowait()["slot_id"] for _ in range(sub.queue.qsize())]
    assert replayed == [2, 3, 4]


# ---- endpoint integration ------------------------------------------------------


@pytest.fixture()
def state() -> AppState:
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    return AppState(
        auth=AuthService(hash_password(PASSWORD)),
        lease=LeaseService(),
        safety=SafetyController(
            object(), TxPlayer(stream_factory=FakeOutputStream), sequencer=sequencer
        ),
        sequencer=sequencer,
        repository=Repository(":memory:"),
        allowed_hosts=frozenset({"testserver"}),
        state_broadcast=StateBroadcaster(),
        decode_broadcast=DecodeBroadcaster(),
        waterfall_fanout=SpectrumFanout(),
    )


@pytest.fixture()
def client(state: AppState) -> TestClient:
    return TestClient(create_app(state), base_url="https://testserver")


def login(client: TestClient) -> str:
    response = client.post("/api/v1/session/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return response.cookies["mrrc_session"]


def test_streams_reject_anonymous_upgrades(client: TestClient) -> None:
    for path in ("/ws/v1/state", "/ws/v1/decodes", "/ws/v1/waterfall"):
        with client.websocket_connect(path) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401


def test_streams_reject_bad_origin(client: TestClient) -> None:
    session_id = login(client)
    with client.websocket_connect(
        "/ws/v1/state",
        headers={
            "cookie": f"mrrc_session={session_id}",
            "origin": "https://evil.example.com",
        },
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401


def test_state_stream_pushes_mutations_and_disconnect_drops_lease(
    client: TestClient, state: AppState
) -> None:
    session_id = login(client)
    cookie = {"cookie": f"mrrc_session={session_id}"}
    with client.websocket_connect("/ws/v1/state", headers=cookie) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "snapshot"
        client.post("/api/v1/lease/acquire", headers=cookie)
        update = ws.receive_json()
        assert update["type"] == "state"
        assert update["lease"]["held"] is True
        assert update["lease"]["mine"] is True  # holder's own broadcast view
        assert state.lease.is_owner(session_id)
    # leaving the stream dropped the lease via the dead-man path (§15.4)
    assert state.lease.current() is None


def test_state_hello_marks_lease_mine_for_the_holder(client: TestClient) -> None:
    """A boot-time WS hello must not clobber the REST mine:true view."""
    session_id = login(client)
    cookie = {"cookie": f"mrrc_session={session_id}"}
    client.post("/api/v1/lease/acquire", headers=cookie)
    with client.websocket_connect("/ws/v1/state", headers=cookie) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "snapshot"
        assert hello["lease"]["held"] is True
        assert hello["lease"]["mine"] is True


def test_state_broadcast_marks_lease_mine_per_connection(
    client: TestClient,
) -> None:
    """One publish, two subscribers: the holder sees mine:true, the
    observer sees mine:false — each frame reflects its own session."""
    holder, observer = login(client), login(client)
    holder_cookie = {"cookie": f"mrrc_session={holder}"}
    observer_cookie = {"cookie": f"mrrc_session={observer}"}
    with (
        client.websocket_connect("/ws/v1/state", headers=holder_cookie) as ws_holder,
        client.websocket_connect("/ws/v1/state", headers=observer_cookie) as ws_observer,
    ):
        assert ws_holder.receive_json()["lease"]["mine"] is False
        assert ws_observer.receive_json()["lease"]["mine"] is False
        client.post("/api/v1/lease/acquire", headers=holder_cookie)
        holder_update = ws_holder.receive_json()
        observer_update = ws_observer.receive_json()
        assert holder_update["lease"]["held"] is True
        assert holder_update["lease"]["mine"] is True
        assert observer_update["lease"]["held"] is True
        assert observer_update["lease"]["mine"] is False


def test_decode_stream_replays_history_then_live(client: TestClient, state: AppState) -> None:
    state.decode_broadcast.publish({"slot_id": 1, "messages": ["CQ K1ABC FN42"]})
    session_id = login(client)
    cookie = {"cookie": f"mrrc_session={session_id}"}
    with client.websocket_connect("/ws/v1/decodes", headers=cookie) as ws:
        replayed = ws.receive_json()
        assert replayed["type"] == "decodes"
        assert replayed["slot_id"] == 1
        state.decode_broadcast.publish({"slot_id": 2, "messages": ["CQ W1AW FN31"]})
        live = ws.receive_json()
        assert live["slot_id"] == 2


def test_waterfall_stream_delivers_binary_frames(client: TestClient, state: AppState) -> None:
    session_id = login(client)
    cookie = {"cookie": f"mrrc_session={session_id}"}
    frame = SpectrumFrame(seq=1, epoch=100.0, bin_hz=2.93, bins=bytes(range(256)))
    with client.websocket_connect("/ws/v1/waterfall", headers=cookie) as ws:
        state.waterfall_fanout.publish(frame)
        payload = ws.receive_bytes()
    again = SpectrumFrame.from_bytes(payload)
    assert again.seq == 1
    assert again.bins == bytes(range(256))
