"""Versioned REST intent endpoints under ``/api/v1`` (§10.1, §11.3).

Control rules (§10.3): reads need a session; TX-starting, radio and
sequencer mutations also need the control lease; STOP needs only a session
and bypasses the lease (NFR-038); diagnostic export needs a recent password
re-authentication (NFR-039).  Every mutation validates Host/Origin
(NFR-035), honors an ``Idempotency-Key`` header and, when
``X-Expected-Revision`` is sent, rejects stale clients with
``stale_revision`` (§10.1).  Rejections carry the stable reasons from §10.1
plus ``tx_active``, ``reauth_required`` and ``rate_limited``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from ..engine.adif import generate_adif
from ..engine.msgparse import ParsedMessage
from ..engine.repository import Repository, VoidWindowExpired
from ..engine.safety import Interlock, SafetyController, TxRefused

log = logging.getLogger("mrrc-ft8.api")
from ..engine.sequencer import DisarmReason, Sequencer
from .auth import AuthService, Session, host_allowed, origin_allowed
from .lease import LeaseService

COOKIE_NAME = "mrrc_session"
COOKIE_MAX_AGE_S = 43_200  # matches the 12 h absolute session cap

REASON_LEASE_REQUIRED = "lease_required"
REASON_STALE_REVISION = "stale_revision"
REASON_INTERLOCK_OPEN = "interlock_open"
REASON_TX_ACTIVE = "tx_active"
REASON_REAUTH_REQUIRED = "reauth_required"
REASON_RATE_LIMITED = "rate_limited"
REASON_UNAUTHENTICATED = "unauthenticated"
REASON_FORBIDDEN = "forbidden"

# Rig level tokens (rigctld ``l``/``L`` commands) are uppercase Hamlib names.
_LEVEL_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
# Mode tokens in rigctld ``M <mode> <passband>`` are uppercase Hamlib names.
_MODE_NAME_RE = re.compile(r"^[A-Z0-9]{1,16}$")

# §10.5 schema-validated settings; safety-impacting ones are TX-locked.
SETTING_SCHEMA: dict[str, Callable[[Any], bool]] = {
    "decoder_profile": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 4,
    "decoder_threads": lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 12,
    "waterfall_lines_per_second": lambda v: isinstance(v, (int, float))
    and not isinstance(v, bool)
    and 1.0 <= float(v) <= 10.0,
    "cq_loop_idle_timeout_s": lambda v: isinstance(v, int) and not isinstance(v, bool) and 60 <= v <= 3600,
}
SAFETY_IMPACTING_SETTINGS = frozenset({"decoder_profile", "decoder_threads"})


class IdempotencyCache:
    """Short-lived key → response replay for retried mutations (§10.1)."""

    def __init__(self, *, ttl_s: float = 600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[str, tuple[float, int, dict[str, Any]]] = {}

    def lookup(self, key: str) -> tuple[int, dict[str, Any]] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, status, body = entry
        if self._clock() - stored_at > self._ttl:
            self._entries.pop(key, None)
            return None
        return status, body

    def store(self, key: str, status: int, body: dict[str, Any]) -> None:
        self._entries[key] = (self._clock(), status, body)
        cutoff = self._clock() - self._ttl
        for stale in [k for k, (t, _, _) in self._entries.items() if t < cutoff]:
            self._entries.pop(stale, None)


@dataclass
class AppState:
    """Composition root shared by the REST and WS layers."""

    auth: AuthService
    lease: LeaseService
    safety: SafetyController
    sequencer: Sequencer
    repository: Repository
    allowed_hosts: frozenset[str]
    my_call: str = ""
    my_grid: str = ""
    rig: Any = None          # RigClient when wired
    orchestrator: Any = None  # running orchestrator when wired
    latency: Any = None      # LatencyHistogram when the DSP path is wired
    tx_driver: Any = None    # TxDriver; always wired (Null encoder without DSP)
    cq_loop: Any = None      # wired by a later task
    state_broadcast: Any = None     # StateBroadcaster when the WS layer runs
    decode_broadcast: Any = None    # DecodeBroadcaster when the WS layer runs
    waterfall_fanout: Any = None    # SpectrumFanout when the WS layer runs
    revision: int = 0
    idempotency: IdempotencyCache = field(default_factory=IdempotencyCache)
    selected: ParsedMessage | None = None
    selected_snr_db: int | None = None
    selected_slot_id: int | None = None  # slot the selected message was heard in
    radio_freq_hz: int | None = None  # last polled dial frequency, if rig is up

    def bump(self) -> int:
        self.revision += 1
        return self.revision

    def actor(self, session: Session) -> str:
        """Audit handle: a session prefix, never the cookie secret (NFR-075)."""

        return f"session-{session.id[:8]}"


def _ok(body: dict[str, Any] | None = None, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, **(body or {})}, status_code=status)


def _reject(status: int, reason: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason, **extra}, status_code=status)


_cty_cache: Any = None


def _cty_database() -> Any:
    """Repository-root cty.dat loaded once; empty db on any failure."""

    global _cty_cache
    if _cty_cache is not None:
        return _cty_cache
    from pathlib import Path

    from ..engine.dxcc import load_cty

    path = Path(__file__).resolve().parents[2] / "cty.dat"
    _cty_cache = load_cty(str(path))
    return _cty_cache


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


async def require_session(request: Request) -> Session:
    state = get_state(request)
    session = state.auth.authenticate(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise _HttpReject(_reject(401, REASON_UNAUTHENTICATED))
    return session


async def validate_mutation(request: Request) -> None:
    """Host/Origin validation plus idempotency/revision for mutations."""

    state = get_state(request)
    host = request.headers.get("host")
    if not host_allowed(host, state.allowed_hosts):
        raise _HttpReject(_reject(403, REASON_FORBIDDEN))
    if not origin_allowed(request.headers.get("origin"), host, state.allowed_hosts):
        raise _HttpReject(_reject(403, REASON_FORBIDDEN))
    expected = request.headers.get("x-expected-revision")
    if expected is not None:
        try:
            if int(expected) != state.revision:
                raise ValueError
        except ValueError:
            raise _HttpReject(
                _reject(409, REASON_STALE_REVISION, revision=state.revision)
            ) from None


class _HttpReject(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


async def require_lease(
    request: Request, session: Session = Depends(require_session)
) -> Session:
    state = get_state(request)
    if not state.lease.is_owner(session.id):
        raise _HttpReject(_reject(409, REASON_LEASE_REQUIRED))
    return session


def create_app(state: AppState) -> FastAPI:
    """Build the FastAPI application for one composition root."""

    app = FastAPI(title="MRRC-FT8", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.app_state = state

    @app.exception_handler(_HttpReject)
    async def _on_reject(_request: Request, exc: _HttpReject) -> JSONResponse:  # type: ignore[misc]
        return exc.response

    app.include_router(create_router(state))
    from .ws import create_ws_router  # deferred: ws imports this module

    app.include_router(create_ws_router(state))

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """The PWA lives under /static; send browsers there (mobile UX)."""

        return RedirectResponse("/static/index.html", status_code=302)

    return app


def create_router(state: AppState) -> APIRouter:
    """Build the ``/api/v1`` router bound to one composition root."""

    router = APIRouter(prefix="/api/v1")

    async def mutate(
        request: Request,
        key: str | None,
        status: int,
        body: dict[str, Any],
    ) -> JSONResponse:
        """Store and return a mutation response for idempotent replay."""

        state.bump()
        if state.state_broadcast is not None:
            state.state_broadcast.publish(_snapshot(state, None))
        response = _ok({**body, "revision": state.revision}, status=status)
        if key:
            state.idempotency.store(key, status, {"ok": True, **body, "revision": state.revision})
        return response

    def replay(request: Request) -> JSONResponse | None:
        key = request.headers.get("idempotency-key")
        if not key:
            return None
        hit = state.idempotency.lookup(key)
        if hit is None:
            return None
        status, body = hit
        return JSONResponse(body, status_code=status, headers={"x-idempotent-replay": "true"})

    # ---- session ---------------------------------------------------------

    @router.post("/session/login")
    async def login(request: Request, response: Response) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(password, str) or not password:
            return _reject(422, "invalid_request")
        result = await asyncio.to_thread(state.auth.login, password)
        if result.session is None:
            client = request.client.host if request.client else "-"
            await asyncio.to_thread(
                state.repository.record_audit,
                actor="anonymous",
                operation="login_failure",
                target=client,
                detail="bad password or rate limited",
            )
            status = 429 if result.retry_after_s > 0 else 401
            return _reject(status, REASON_RATE_LIMITED if status == 429 else REASON_UNAUTHENTICATED,
                           retry_after_s=round(result.retry_after_s, 2))
        cookie = JSONResponse({"ok": True, "session": _session_view(result.session)})
        cookie.set_cookie(
            COOKIE_NAME,
            result.session.id,
            max_age=COOKIE_MAX_AGE_S,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return cookie

    @router.post("/session/logout")
    async def logout(request: Request) -> JSONResponse:
        await validate_mutation(request)
        state.auth.logout(request.cookies.get(COOKIE_NAME))
        response = _ok()
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @router.post("/session/reauth")
    async def reauth(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(password, str):
            return _reject(422, "invalid_request")
        ok = await asyncio.to_thread(state.auth.reauthenticate, session.id, password)
        if not ok:
            return _reject(403, REASON_REAUTH_REQUIRED)
        return _ok({"reauth_until_s": state.auth.reauth_window_s})

    @router.get("/session/current")
    async def current_session(session: Session = Depends(require_session)) -> JSONResponse:
        return _ok({"session": _session_view(session)})

    # ---- lease ------------------------------------------------------------

    @router.get("/lease")
    async def lease_view(session: Session = Depends(require_session)) -> dict[str, Any]:
        lease = state.lease.current()
        return {
            "held": lease is not None,
            "mine": lease is not None and lease.session_id == session.id,
            "expires_in_s": state.lease.expires_in(),
        }

    @router.post("/lease/acquire")
    async def lease_acquire(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        lease = state.lease.acquire(session.id)
        if lease is None:
            return _reject(409, REASON_LEASE_REQUIRED)  # held by another session
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"lease": "held"})

    @router.post("/lease/heartbeat")
    async def lease_heartbeat(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        await validate_mutation(request)
        if state.lease.heartbeat(session.id) is None:
            return _reject(409, REASON_LEASE_REQUIRED)
        return _ok({"lease": "held"})

    @router.post("/lease/release")
    async def lease_release(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        state.lease.release(session.id)
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"lease": "free"})

    # ---- state / health -------------------------------------------------------

    @router.get("/state")
    async def state_snapshot(session: Session = Depends(require_session)) -> JSONResponse:
        # _ok envelope: boot() gates on ``snapshot.ok`` before applying the
        # initial snapshot (worked_calls drives hide-already-worked); a bare
        # dict made the gate always falsy and the client relied on the WS
        # hello alone.
        return _ok(_snapshot(state, session))

    @router.get("/health")
    async def health(session: Session = Depends(require_session)) -> dict[str, Any]:
        return _health(state)

    # ---- operation ----------------------------------------------------------

    @router.post("/operation/select")
    async def operation_select(
        request: Request, session: Session = Depends(require_lease)
    ) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("dx_call"), str) or not body["dx_call"]:
            return _reject(422, "invalid_request")
        # Selecting never arms or transmits (§15.6).
        state.selected = ParsedMessage(
            text=str(body.get("text") or body["dx_call"]),
            is_cq=bool(body.get("is_cq", False)),
            from_call=body["dx_call"].upper(),
            grid=str(body.get("dx_grid") or "").upper(),
        )
        state.selected_snr_db = body.get("snr_db") if isinstance(body.get("snr_db"), int) else None
        state.selected_slot_id = body.get("slot_id") if isinstance(body.get("slot_id"), int) else None
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"selected": state.selected.from_call})

    @router.post("/operation/reply")
    async def operation_reply(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        if state.selected is None or state.selected_snr_db is None:
            return _reject(409, "no_selection")
        try:
            await state.safety.arm()
        except TxRefused as exc:
            return _reject(409, REASON_INTERLOCK_OPEN, detail=str(exc))
        # UC-003: transmit on the slot opposite the one the partner's message
        # was heard in; a message without a known slot defaults to even.
        slot_id = state.selected_slot_id
        tx_phase = 0 if slot_id is None else 1 - (slot_id % 2)
        state.sequencer.reply_to(state.selected, state.selected_snr_db, tx_phase=tx_phase)
        await _audit(state, session, "reply", state.selected.from_call, "")
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"sequencer": state.sequencer.state.value})

    @router.post("/operation/cq")
    async def operation_cq(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        content_length = request.headers.get("content-length")
        body = await request.json() if content_length and content_length != "0" else {}
        if not isinstance(body, dict):
            return _reject(422, "invalid_request")
        loop = bool(body.get("loop", False))
        if loop:
            if state.cq_loop is None:
                return _reject(503, "cq_loop_unavailable")
            await state.cq_loop.start()  # arms via safety; refusal is audited
            if not state.cq_loop.active:
                return _reject(409, REASON_INTERLOCK_OPEN)
        else:
            try:
                await state.safety.arm()
            except TxRefused as exc:
                return _reject(409, REASON_INTERLOCK_OPEN, detail=str(exc))
            state.sequencer.start_cq()
        await _audit(state, session, "cq", "", "loop" if loop else "")
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"sequencer": state.sequencer.state.value})

    @router.post("/operation/enable_tx_off")
    async def operation_enable_tx_off(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        state.safety.disarm(DisarmReason.MANUAL)
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"armed": False})

    @router.post("/operation/clear-fault")
    async def operation_clear_fault(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        """Release latched interlock faults after the operator verified the
        repair (§15.5): without this path a transient fault locks TX until
        process restart.  Clearing never arms by itself."""

        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        content_length = request.headers.get("content-length")
        body = await request.json() if content_length and content_length != "0" else {}
        if not isinstance(body, dict):
            return _reject(422, "invalid_request")
        raw = body.get("interlock")
        if raw is not None and (not isinstance(raw, str) or raw not in Interlock):
            return _reject(422, "invalid_request")
        targets = (
            [Interlock(raw)]
            if raw is not None
            else sorted(state.safety.faults, key=lambda f: f.value)
        )
        for interlock in targets:
            state.safety.clear_fault(interlock)
        scope = raw if raw is not None else f"all ({len(targets)} latched)"
        await _audit(state, session, "clear_fault", "", scope)
        return await mutate(
            request, request.headers.get("idempotency-key"), 200, {"safety": state.safety.health}
        )

    @router.post("/operation/stop")
    async def operation_stop(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        """Priority STOP: any authenticated session, no lease (NFR-038)."""

        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        await state.safety.stop_tx(reason=f"api:{state.actor(session)}")
        await _audit(state, session, "stop", "", "priority STOP TX")
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"stopped": True})

    @router.post("/radio/band")
    async def radio_band(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        freq = body.get("freq_hz") if isinstance(body, dict) else None
        if not isinstance(freq, int) or isinstance(freq, bool) or not 100_000 <= freq <= 450_000_000:
            return _reject(422, "invalid_request")
        if state.safety.armed or state.safety.ptt_on:
            return _reject(409, REASON_TX_ACTIVE)
        if state.rig is None:
            return _reject(503, "rig_unavailable")
        try:
            await state.rig.set_frequency(freq)
        except Exception as exc:
            return _reject(502, "rig_error", detail=str(exc))
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"freq_hz": freq})

    # rigctld level access (ATT / AGC / PREAMP / RF gain …).  Levels are
    # per-rig: unsupported ones answer ``rig_unsupported`` so the settings
    # drawer can show them greyed out instead of failing the whole request.
    # NB: some rig models (FT-710) never answer the ``L <name>`` query —
    # every attempt times out and drops the session, which would corrupt
    # concurrent rig_poll/filter traffic.  Query results are therefore
    # cached: once a level fails, it is treated as unsupported for a while
    # instead of hammering the rig on every drawer open.
    @router.get("/radio/rig/levels")
    async def radio_rig_levels(session: Session = Depends(require_session)) -> JSONResponse:
        if state.rig is None:
            return _reject(503, "rig_unavailable")
        now = time.monotonic()
        cache = getattr(state, "_rig_level_cache", None)
        if cache is None:
            cache = state._rig_level_cache = {"at": 0.0, "levels": None}
        if cache["levels"] is not None and now - cache["at"] < 60.0:
            return _ok({"levels": cache["levels"]})
        wanted = ("ATT", "PREAMP", "RF", "AGC")

        async def _read(name: str) -> tuple[str, float | None]:
            try:
                return name, await state.rig.get_level(name)
            except Exception:
                return name, None

        # Serialise and stop at the first failure: on rigs that never answer
        # ``L`` (FT-710) one probe is enough to mark all levels unsupported —
        # no point paying 4 × timeout or dropping the session 4 times.
        levels: dict[str, float | None] = {}
        for name in wanted:
            key, value = await _read(name)
            levels[key] = value
            if value is None:
                for rest in wanted[wanted.index(name) + 1:]:
                    levels[rest] = None
                break
        state._rig_level_cache = {"at": now, "levels": levels}
        return _ok({"levels": levels})

    # Filter bandwidth (FT-710 supports 1.8/2.4/3.0 kHz on USB/LSB).
    # Returns the current pair so the drawer can show the rig's actual
    # passband, not a stale local guess.  The passband comes from the rig's
    # SH register, not hamlib's ``m``: hamlib 4.6.2 has no FT-710 branch in
    # newcat_get_rx_bandwidth and misreports 2400 Hz as 1800 Hz.
    @router.get("/radio/mode")
    async def radio_mode(session: Session = Depends(require_session)) -> JSONResponse:
        if state.rig is None:
            return _reject(503, "rig_unavailable")
        try:
            mode, passband_hz = await state.rig.get_mode()
        except Exception as exc:
            log.warning("radio mode read failed: %s", exc, exc_info=True)
            return _reject(502, "rig_error", detail=str(exc))
        try:
            passband_hz = await state.rig.get_filter_width()
        except Exception as exc:
            # Rig without raw SH access, or a transient protocol hiccup:
            # fall back to the hamlib-reported passband.
            log.debug("filter width read fell back to hamlib passband: %s", exc)
        return _ok({"mode": mode, "passband_hz": passband_hz})

    @router.post("/radio/mode")
    async def radio_mode_set(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        mode = body.get("mode") if isinstance(body, dict) else None
        passband_hz = body.get("passband_hz") if isinstance(body, dict) else None
        if not isinstance(mode, str) or not _MODE_NAME_RE.match(mode):
            return _reject(422, "invalid_request")
        if not isinstance(passband_hz, int) or isinstance(passband_hz, bool) \
                or not 0 <= passband_hz <= 100_000:
            return _reject(422, "invalid_request")
        if state.safety.armed or state.safety.ptt_on:
            return _reject(409, REASON_TX_ACTIVE)
        if state.rig is None:
            return _reject(503, "rig_unavailable")
        try:
            await state.rig.set_mode(mode, passband_hz)
        except Exception as exc:
            return _reject(502, "rig_error", detail=str(exc))
        # hamlib 4.6.2 never applies the width on the FT-710 (its SH frame
        # is mis-formatted, see RigClient.set_filter_width), so apply it via
        # the raw path too.  Best effort: the mode set already succeeded.
        try:
            await state.rig.set_filter_width(passband_hz)
        except ValueError:
            pass  # not one of the FT-710 raw-table widths
        except Exception as exc:
            log.warning("mode set ok but filter width apply failed: %s", exc)
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"mode": mode, "passband_hz": passband_hz})

    @router.post("/radio/filter")
    async def radio_filter(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        """Set FT-710 filter width (1800/2400/3000 Hz).

        hamlib 4.6.2's ``M <mode> <pb>`` never changes the FT-710 width
        (backend mis-frames the SH command), so the correctly framed
        ``SH00<NN>;`` CAT command is forwarded through rigctld's
        ``\\send_raw`` (AD-008: rigctld stays the serial owner).
        """

        await validate_mutation(request)
        body = await request.json()
        hz = body.get("hz") if isinstance(body, dict) else None
        if not isinstance(hz, int) or hz not in (1800, 2400, 3000):
            return _reject(422, "invalid_request")
        if state.safety.armed or state.safety.ptt_on:
            return _reject(409, REASON_TX_ACTIVE)
        if state.rig is None:
            return _reject(503, "rig_unavailable")
        try:
            await state.rig.set_filter_width(hz)
        except Exception as exc:
            log.warning("filter set failed: %s", exc, exc_info=True)
            return _reject(502, "rig_error", detail=str(exc))
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"filter_hz": hz})

    @router.post("/radio/rig/level")
    async def radio_rig_level(request: Request, session: Session = Depends(require_lease)) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        name = body.get("level") if isinstance(body, dict) else None
        value = body.get("value") if isinstance(body, dict) else None
        if not isinstance(name, str) or not _LEVEL_NAME_RE.match(name):
            return _reject(422, "invalid_request")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return _reject(422, "invalid_request")
        if state.safety.armed or state.safety.ptt_on:
            return _reject(409, REASON_TX_ACTIVE)
        if state.rig is None:
            return _reject(503, "rig_unavailable")
        try:
            await state.rig.set_level(name, float(value))
        except Exception as exc:
            return _reject(502, "rig_error", detail=str(exc))
        # The rig state changed: drop the 60 s capability snapshot so the
        # next drawer open re-reads the real level values instead of a stale
        # one (the FT-710 raw-CAT reads are cheap and reliable).
        state._rig_level_cache = {"at": 0.0, "levels": None}
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"level": name, "value": value})

    # ---- logs ----------------------------------------------------------------

    @router.get("/logs/qsos")
    async def logs_qsos(session: Session = Depends(require_session)) -> JSONResponse:
        qsos = await asyncio.to_thread(state.repository.list_qsos, since_days=7)
        # _ok envelope: the drawer's api.qsos() gate is ``res.ok`` (same as
        # every mutation); a bare dict made the log overlay always take the
        # error branch even on 200 ("Could not load log: 200").
        return _ok({"qsos": [_qso_view(q) for q in qsos], "revision": state.revision})

    @router.post("/logs/qsos/{qso_id}/void")
    async def logs_void(request: Request, qso_id: int, session: Session = Depends(require_session)) -> JSONResponse:
        await validate_mutation(request)
        if (hit := replay(request)) is not None:
            return hit
        body = await request.json() if request.headers.get("content-length") else {}
        reason = body.get("reason", "") if isinstance(body, dict) else ""
        try:
            await asyncio.to_thread(
                state.repository.void_qso,
                qso_id,
                actor=state.actor(session),
                reason=str(reason),
            )
        except KeyError:
            return _reject(404, "not_found")
        except VoidWindowExpired:
            return _reject(409, "void_window_expired")
        except ValueError as exc:
            return _reject(409, "invalid_state", detail=str(exc))
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"voided": qso_id})

    @router.get("/logs/adif")
    async def logs_adif(session: Session = Depends(require_session)) -> PlainTextResponse:
        qsos = await asyncio.to_thread(
            state.repository.list_qsos, include_void=False, since_days=7
        )
        document = await asyncio.to_thread(generate_adif, qsos)
        return PlainTextResponse(
            document,
            media_type="text/plain",
            headers={"content-disposition": 'attachment; filename="mrrc-ft8.adi"'},
        )

    # ---- DXCC -------------------------------------------------------------

    @router.get("/dxcc")
    async def dxcc(session: Session = Depends(require_session)) -> JSONResponse:
        from ..engine.dxcc import dxcc_summary

        summary = await asyncio.to_thread(
            dxcc_summary, state.repository, _cty_database()
        )
        return _ok(summary.to_dict())

    # ---- diagnostics ---------------------------------------------------------

    @router.get("/diagnostics/health")
    async def diagnostics_health(session: Session = Depends(require_session)) -> dict[str, Any]:
        return _health(state)

    @router.post("/diagnostics/export")
    async def diagnostics_export(request: Request, session: Session = Depends(require_session)) -> Response:
        await validate_mutation(request)
        if not state.auth.has_recent_reauth(session.id):
            return _reject(403, REASON_REAUTH_REQUIRED)
        bundle = await asyncio.to_thread(_build_diagnostic_bundle, state)
        await _audit(state, session, "diagnostic_export", "", "raw bundle")
        return Response(
            content=bundle,
            media_type="application/gzip",
            headers={"content-disposition": 'attachment; filename="mrrc-ft8-diagnostics.tar.gz"'},
        )

    # ---- settings ------------------------------------------------------------

    @router.get("/settings")
    async def settings_view(session: Session = Depends(require_session)) -> dict[str, Any]:
        values = await asyncio.to_thread(_read_settings, state)
        return {"settings": values, "schema": sorted(SETTING_SCHEMA)}

    @router.put("/settings")
    async def settings_update(request: Request, session: Session = Depends(require_session)) -> JSONResponse:
        await validate_mutation(request)
        body = await request.json()
        if not isinstance(body, dict) or not body:
            return _reject(422, "invalid_request")
        for key, value in body.items():
            validator = SETTING_SCHEMA.get(key)
            if validator is None or not validator(value):
                return _reject(422, "invalid_setting", setting=key)
        if (
            state.safety.armed or state.safety.ptt_on
        ) and SAFETY_IMPACTING_SETTINGS.intersection(body):
            return _reject(409, REASON_TX_ACTIVE)  # §10.5: safety-impacting locked during TX
        for key, value in body.items():
            await asyncio.to_thread(state.repository.set_setting, key, value)
        return await mutate(request, request.headers.get("idempotency-key"), 200, {"updated": sorted(body)})

    return router


# ---- helpers -----------------------------------------------------------------


def _session_view(session: Session) -> dict[str, Any]:
    return {
        "created_epoch": session.created_epoch,
        "reauth_recent": session.reauth_epoch is not None,
    }


def _snapshot(state: AppState, session: Session | None) -> dict[str, Any]:
    lease = state.lease.current()
    sequencer = state.sequencer
    snapshot: dict[str, Any] = {
        "revision": state.revision,
        "lease": {
            "held": lease is not None,
            "mine": session is not None
            and lease is not None
            and lease.session_id == session.id,
        },
        "safety": state.safety.health,
        "sequencer": {
            "state": sequencer.state.value,
            "tx_enabled": sequencer.tx_enabled,
            "dx_call": sequencer.dx_call,
            "cq_loop": (
                state.cq_loop.status()
                if state.cq_loop is not None
                else {"active": False, "idle_remaining_s": 0}
            ),
        },
        "selected": (
            None
            if state.selected is None
            else {"call": state.selected.from_call, "grid": state.selected.grid}
        ),
        "radio": {"freq_hz": state.radio_freq_hz},
        "station": {
            "my_call": state.my_call,
            "my_grid": state.my_grid,
            "worked_calls": sorted(state.repository.worked_calls()),
        },
    }
    if state.orchestrator is not None:
        counters = state.orchestrator.counters
        snapshot["orchestrator"] = {
            "slots_started": counters.slots_started,
            "slots_skipped": counters.slots_skipped,
            "decodes": counters.decodes,
            "deadline_misses": counters.deadline_misses,
        }
    return snapshot


def _health(state: AppState) -> dict[str, Any]:
    health: dict[str, Any] = {
        "safety": state.safety.health,
        "lease_held": state.lease.current() is not None,
        "sessions": state.auth.session_count,
    }
    if state.latency is not None:
        health["decode_latency"] = state.latency.snapshot()
    if state.orchestrator is not None:
        counters = state.orchestrator.counters
        health["deadline_misses"] = counters.deadline_misses
    return health


async def _audit(
    state: AppState, session: Session, operation: str, target: str, detail: str
) -> None:
    await asyncio.to_thread(
        state.repository.record_audit,
        actor=state.actor(session),
        operation=operation,
        target=target,
        detail=detail,
    )


def _qso_view(qso: Any) -> dict[str, Any]:
    return {
        "id": qso.id,
        "my_call": qso.my_call,
        "dx_call": qso.dx_call,
        "dx_grid": qso.dx_grid,
        "report_sent": qso.report_sent,
        "report_rcvd": qso.report_rcvd,
        "started_utc": qso.started_utc,
        "mode": qso.mode,
        "freq_hz": qso.freq_hz,
        "band": qso.band,
        "status": qso.status.value,
        "completed_epoch": qso.completed_epoch,
    }


def _read_settings(state: AppState) -> dict[str, Any]:
    return {key: state.repository.get_setting(key) for key in sorted(SETTING_SCHEMA)}


def _build_diagnostic_bundle(state: AppState) -> bytes:
    """Raw local bundle; auth secrets and cookie values are never included."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in {
            "health.json": _health(state),
            "settings.json": _read_settings(state),
            "qsos.json": [_qso_view(q) for q in state.repository.list_qsos()],
            "audit.json": state.repository.audit_events(),
        }.items():
            data = json.dumps(payload, indent=2, default=str).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
