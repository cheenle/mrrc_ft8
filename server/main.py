"""Lifespan composition and Uvicorn entry point (§11.3, §12.3).

Startup order (§12.3): interrupted QSOs become ``ABORTED_RESTART``, the
safety controller starts monitor-only with best-effort PTT-off, audio and
the UTC orchestrator start in monitor mode, and only then does the web
layer serve.  Shutdown runs priority STOP before audio/rig/worker teardown.
The dead-man callback and lease events are wired to the safety controller
and the state stream here, at the one composition root.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

os.environ.setdefault("OMP_STACKSIZE", "10M")  # before NumPy/OpenMP loads

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import uvicorn
import numpy as np
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .engine.audio_rx import CaptureHealthMonitor, UtcRing
from .engine.capture_proc import CaptureProcess
from .engine.dsp_decode import SupervisorDecoder
from .engine.latency import LatencyHistogram
from .engine.orchestrator import Orchestrator
from .engine.repository import Repository
from .engine.rig import RigClient
from .engine.safety import Interlock, SafetyController
from .engine.sequencer import DisarmReason, QsoContext, QSOState, Sequencer
from .engine.bands import band_from_freq_hz
from .engine.audio_tx import TxPlayer
from .engine.waterfall import SpectrumComputer, SpectrumFanout
from .web.api import AppState, create_app, _snapshot
from .web.auth import AuthService
from .web.lease import LeaseEventKind, LeaseService
from .web.ws import DecodeBroadcaster, StateBroadcaster

LEASE_POLL_S = 1.0
MAINTENANCE_S = 3_600.0
JTDX_SYNC_S = 3_600.0

log = logging.getLogger("mrrc-ft8")
log.setLevel(os.environ.get("MRRC_FT8_LOG_LEVEL", "INFO").upper())
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)


class _NullEncoder:
    """TX encoder placeholder when DSP is disabled; every encode fails fast."""

    async def encode(self, message: str, frequency: float, *, slot_id: int) -> Any:
        from .engine.dsp_encode import TxEncodeError

        raise TxEncodeError("dsp_unavailable", "TX encoder requires the DSP worker")


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Runtime configuration (§12.6); secrets arrive via the environment."""

    password_hash: str
    my_call: str
    my_grid: str
    allowed_hosts: frozenset[str]
    db_path: str = "mrrc-ft8.db"
    pending_path: str = "data/qso-pending.jsonl"
    jtdx_log_path: str | None = None
    rigctld_host: str = "127.0.0.1"
    rigctld_port: int = 4532
    audio_device: int | str | None = None
    decoder_profile: int = 3
    decoder_threads: int = 0  # 0 = Auto: clamp(cpu_count - 1, 1, 12) (I9, §12.6)

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Load configuration; safety-impacting gaps fail startup (§12.6)."""

        password_hash = os.environ.get("MRRC_FT8_PASSWORD_HASH", "")
        my_call = os.environ.get("MRRC_FT8_MY_CALL", "").upper()
        my_grid = os.environ.get("MRRC_FT8_MY_GRID", "").upper()
        missing = [
            name
            for name, value in (
                ("MRRC_FT8_PASSWORD_HASH", password_hash),
                ("MRRC_FT8_MY_CALL", my_call),
                ("MRRC_FT8_MY_GRID", my_grid),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing required configuration: {', '.join(missing)}")
        hosts = frozenset(
            h.strip().lower()
            for h in os.environ.get("MRRC_FT8_ALLOWED_HOSTS", "localhost").split(",")
            if h.strip()
        )
        rig_host, _, rig_port = os.environ.get(
            "MRRC_FT8_RIGCTLD", "127.0.0.1:4532"
        ).partition(":")
        jtdx_log_path = os.environ.get("MRRC_FT8_JTDX_LOG_PATH", "").strip() or None
        audio_raw = os.environ.get("MRRC_FT8_AUDIO_DEVICE", "")
        audio_device: int | str | None = None
        if audio_raw:
            audio_device = int(audio_raw) if audio_raw.isdigit() else audio_raw
        try:
            profile = int(os.environ.get("MRRC_FT8_DECODER_PROFILE", "3"))
        except ValueError:
            raise ValueError("MRRC_FT8_DECODER_PROFILE must be an integer 0..4")
        if not 0 <= profile <= 4:
            raise ValueError("MRRC_FT8_DECODER_PROFILE must be 0..4")
        threads_raw = os.environ.get("MRRC_FT8_DECODER_THREADS", "auto").lower()
        if threads_raw == "auto":
            threads = 0
        else:
            try:
                threads = int(threads_raw)
            except ValueError:
                raise ValueError(
                    "MRRC_FT8_DECODER_THREADS must be 'auto' or an integer 1..12"
                )
            if not 1 <= threads <= 12:
                raise ValueError("MRRC_FT8_DECODER_THREADS must be 1..12")
        return cls(
            password_hash=password_hash,
            my_call=my_call,
            my_grid=my_grid,
            allowed_hosts=hosts or frozenset({"localhost"}),
            db_path=os.environ.get("MRRC_FT8_DB_PATH", "mrrc-ft8.db"),
            pending_path=os.environ.get(
                "MRRC_FT8_PENDING_PATH", "data/qso-pending.jsonl"
            ),
            jtdx_log_path=jtdx_log_path,
            rigctld_host=rig_host,
            rigctld_port=int(rig_port or 4532),
            audio_device=audio_device,
            decoder_profile=profile,
            decoder_threads=threads,
        )


def decode_message_view(
    message: Any, my_call: str = "", *, is_new_dxcc: bool = False
) -> dict[str, Any]:
    """One decode message → wire payload (Band Activity columns)."""

    from .engine.msgparse import addressed_to, base_call

    parsed = message.parsed
    return {
        "text": message.result.text,
        "snr": message.result.snr,
        "dt": message.result.dt,
        "freq": message.result.frequency,
        "call": parsed.from_call,
        "grid": parsed.grid,
        "is_cq": parsed.is_cq,
        "to_me": addressed_to(parsed, my_call),
        # The station's own transmitted message echoed back in the RX slot.
        "mine": bool(parsed.from_call)
        and bool(my_call)
        and base_call(parsed.from_call) == base_call(my_call),
        "is_new_dxcc": is_new_dxcc,
    }


def auto_call_candidate(
    view: dict[str, Any],
    *,
    sequencer_state: str,
    has_selection: bool,
    auto_call_enabled: bool,
) -> bool:
    """Decision A/B: auto-QSO the first new-DXCC CQ when idle, no manual
    selection, and the backend switch is on.  Never interrupts a QSO."""

    return (
        auto_call_enabled
        and not has_selection
        and sequencer_state == QSOState.IDLE.value
        and bool(view.get("is_new_dxcc"))
        and bool(view.get("is_cq"))
        and not view.get("mine")
    )


async def _auto_call(
    state: Any, repository: Any, view: dict[str, Any], slot_id: int | None, tx_phase: int
) -> None:
    """Arm TX (interlock-gated) then drive the full QSO via the sequencer.

    System-level: no control lease is taken (unattended), but safety.arm is
    the single TX gate — a refusal (fault) skips this slot and the next one
    retries.  Audit row ``auto_call`` records the intent.
    """

    from .engine.msgparse import ParsedMessage
    from .engine.safety import TxRefused

    call = str(view.get("call") or "")
    try:
        await state.safety.arm()
    except TxRefused:
        log.info("auto_call skipped: interlock open (%s)", call)
        return
    except Exception:
        log.exception("auto_call arm failed (%s)", call)
        return
    try:
        state.selected = ParsedMessage(
            text=str(view.get("text") or call),
            is_cq=True,
            from_call=call.upper(),
            grid=str(view.get("grid") or "").upper(),
        )
        state.selected_snr_db = view.get("snr")
        state.selected_slot_id = slot_id
        state.sequencer.reply_to(state.selected, view.get("snr"), tx_phase=tx_phase)
        await asyncio.to_thread(
            repository.record_audit,
            actor="system",
            operation="auto_call",
            target=call,
            detail=f"snr={view.get('snr')} new_dxcc",
        )
        log.info("auto_call: %s snr=%s slot=%d", call, view.get("snr"), slot_id)
    except Exception:
        log.exception("auto_call failed for %s", call)


def create_server(
    config: ServerConfig,
    *,
    rig: Any = None,
    start_dsp: bool = True,
    start_audio: bool = True,
) -> FastAPI:
    """Compose the full application around one :class:`AppState`."""

    repository = Repository(config.db_path)
    rig_client = rig if rig is not None else RigClient(
        host=config.rigctld_host, port=config.rigctld_port
    )
    sequencer = Sequencer(my_call=config.my_call, my_grid=config.my_grid)
    player = TxPlayer(device=config.audio_device)

    def on_safety_event(event: Any) -> None:
        # TX arm/key/fault transitions are otherwise invisible outside the
        # state broadcast; log them so field diagnosis does not need a client.
        log.info("safety %s: %s", event.kind.value, event.detail)

    safety = SafetyController(
        rig_client, player, sequencer=sequencer, on_event=on_safety_event
    )
    state = AppState(
        auth=AuthService(config.password_hash),
        lease=LeaseService(),
        safety=safety,
        sequencer=sequencer,
        repository=repository,
        my_call=config.my_call,
        my_grid=config.my_grid,
        rig=rig_client,
        allowed_hosts=config.allowed_hosts,
        state_broadcast=StateBroadcaster(),
        decode_broadcast=DecodeBroadcaster(),
        waterfall_fanout=SpectrumFanout(),
    )

    # Dead-man and lease audit wiring (§15.4, §10.6).  Callbacks may fire
    # from any thread, so coroutines are scheduled onto the lifespan loop.
    main_loop: list[asyncio.AbstractEventLoop] = []

    def schedule(coro: Any) -> None:
        if main_loop:
            main_loop[0].call_soon_threadsafe(lambda: asyncio.create_task(coro))
        else:
            coro.close()  # before startup: nothing to schedule on

    def on_dead_man(_session_id: str, reason: str) -> None:
        schedule(safety.stop_tx(reason))

    def on_lease_event(event: Any) -> None:
        state.state_broadcast.publish(_snapshot(state, None))
        if event.kind == LeaseEventKind.RELEASE:
            # A deliberate release must not leave TX armed and unattended
            # (I1): drop the armed flag and stop the sequencer.  Expiry and
            # disconnect already run the dead-man priority STOP, so only
            # RELEASE needs this; ``disarm`` is synchronous and never
            # touches PTT, so no scheduling is required.
            safety.disarm(DisarmReason.MANUAL)
        schedule(
            asyncio.to_thread(
                repository.record_audit,
                actor=f"session-{event.session_id[:8]}",
                operation=f"lease_{event.kind.value}",
                target="",
                detail="",
            )
        )

    state.lease = LeaseService(on_dead_man=on_dead_man, on_event=on_lease_event)

    def report_dsp_fault(context: str, error: Exception) -> None:
        # DSP faults latch in the safety controller: a dead Worker failing
        # every slot is reported once, not once per slot (I2).
        log.error("dsp fault: %s: %s", context, error)
        schedule(safety.report_fault(Interlock.DSP, f"{context}: {error}"))

    # CQ loop wiring (§6 auto-CQ): polls on the lease watchdog; idle timeout
    # is an operator setting, audit entries go through the repository.
    from .engine.cq_loop import DEFAULT_IDLE_TIMEOUT_S, CqLoopController

    def cq_loop_idle_timeout() -> int:
        value = repository.get_setting("cq_loop_idle_timeout_s")
        return int(value) if isinstance(value, int) else DEFAULT_IDLE_TIMEOUT_S

    def cq_loop_audit(operation: str, detail: str) -> None:
        schedule(
            asyncio.to_thread(
                repository.record_audit,
                actor="system",
                operation=operation,
                target="",
                detail=detail,
            )
        )

    state.cq_loop = CqLoopController(
        sequencer,
        arm=safety.arm,
        lease_alive=lambda: state.lease.current() is not None,
        clock=time.monotonic,
        idle_timeout=cq_loop_idle_timeout,
        on_audit=cq_loop_audit,
    )

    capture: CaptureProcess | None = None
    orchestrator: Orchestrator | None = None
    supervisor: Any = None
    supervisor_decoder: SupervisorDecoder | None = None
    encoder: Any = None
    ring: UtcRing | None = None
    tasks: list[asyncio.Task[Any]] = []

    if start_audio:
        ring = UtcRing()
        computer = SpectrumComputer()

        def waterfall_tap(samples: Any, epoch: float) -> None:
            for frame in computer.push(samples, epoch):
                state.waterfall_fanout.publish(frame)

        capture = CaptureProcess(ring, device=config.audio_device, tap=waterfall_tap)

    if start_dsp:
        from .core.models import DecodeConfig, auto_thread_count
        from .core.supervisor import WorkerSupervisor
        from .engine.dsp_encode import SupervisorEncoder
        from .engine.tx_driver import TxDriver

        state.latency = LatencyHistogram()
        supervisor = WorkerSupervisor()
        decoder_config = DecodeConfig(
            profile=config.decoder_profile,
            threads=config.decoder_threads or auto_thread_count(),
        )
        supervisor_decoder = SupervisorDecoder(
            supervisor, decoder_config, histogram=state.latency
        )
        encoder = SupervisorEncoder(supervisor)
        state.tx_driver = TxDriver(sequencer, encoder, safety)
        state.tx_driver.on_tx_error = lambda slot_id, error: report_dsp_fault(  # type: ignore[method-assign]
            f"tx slot {slot_id}", error
        )
        slot_ring = ring if ring is not None else UtcRing()
        capture_health = CaptureHealthMonitor()
        slot_rms: dict[int, float] = {}
        capture_bounces = 0
        MAX_CAPTURE_BOUNCES = 3

        async def recover_capture(rms: float) -> None:
            """Latch AUDIO and reopen the capture stream in monitor state.

            A degraded USB audio session (time-shifted / starved / looping
            content with healthy ring metrics) never heals itself, while a
            freshly opened stream is always clean (2026-08-02 field
            findings).  TX stays disarmed until the operator clears the
            fault — no recovery auto-resumes TX (§12).
            """

            nonlocal capture_bounces
            capture_bounces += 1
            log.critical(
                "capture session degraded: hot band (rms %.0f) but zero decodes",
                rms,
            )
            await safety.report_fault(
                Interlock.AUDIO,
                "degraded capture session: hot band, zero decodes",
            )
            if capture_bounces <= MAX_CAPTURE_BOUNCES:
                log.critical(
                    "restarting capture process (%d/%d)",
                    capture_bounces,
                    MAX_CAPTURE_BOUNCES,
                )
                await asyncio.to_thread(capture.restart)
            else:
                log.critical(
                    "capture bounce limit reached; manual intervention required"
                )

        def read_slot_logged(slot_id: int) -> bytes | None:
            data = slot_ring.read_slot(slot_id)
            if data is not None:
                pcm = np.frombuffer(data, dtype="<i2")
                slot_rms.clear()
                slot_rms[slot_id] = float(
                    np.sqrt(np.mean(pcm.astype(np.float64) ** 2))
                )
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "ring slot %d: %s base=%s high=%s gaps=%d dropped=%d caprestarts=%d",
                    slot_id,
                    "hit" if data is not None else "MISS",
                    slot_ring.base,
                    slot_ring.high_water,
                    slot_ring.gap_count,
                    slot_ring.metrics.dropped_samples,
                    capture.restart_count if capture is not None else -1,
                )
            return data

        def on_decode(slot_decode: Any) -> None:
            nonlocal capture_bounces
            if capture is not None:
                rms = slot_rms.get(slot_decode.slot_id, 0.0)
                log.debug(
                    "monitor feed: slot %d rms=%.0f msgs=%d",
                    slot_decode.slot_id,
                    rms,
                    len(slot_decode.messages),
                )
                if slot_decode.messages:
                    capture_bounces = 0  # healthy session: reset self-heal budget
                if capture_health.observe(rms, len(slot_decode.messages)):
                    asyncio.get_running_loop().create_task(recover_capture(rms))
            from .engine.dxcc import get_cty_database

            # is_new_dxcc: entity worked-entity check against the DXCC cache
            # (pre-filled at startup; conservative False while unbuilt).
            worked_dxcc = (
                {e.name for e in state.dxcc_cache.entities}
                if state.dxcc_cache is not None
                else None
            )
            views = []
            for message in slot_decode.messages:
                view = decode_message_view(message, config.my_call)
                if view["call"] and not view["mine"] and worked_dxcc is not None:
                    entity = get_cty_database().lookup(view["call"])
                    view["is_new_dxcc"] = bool(entity) and entity[0] not in worked_dxcc
                views.append(view)
            batch = {
                "slot_id": slot_decode.slot_id,
                "late": slot_decode.late,
                "messages": views,
            }
            log.info("slot %d: %d msgs, %d subs, %d started, %d skipped",
                     slot_decode.slot_id, len(slot_decode.messages),
                     len(state.decode_broadcast._subscribers),
                     orchestrator.counters.slots_started if orchestrator else -1,
                     orchestrator.counters.slots_skipped if orchestrator else -1)
            if log.isEnabledFor(logging.DEBUG):
                for message in slot_decode.messages:
                    log.debug(
                        "slot %d msg: snr=%d dt=%+.1f f=%.0f %s",
                        slot_decode.slot_id, message.result.snr,
                        message.result.dt, message.result.frequency,
                        message.result.text,
                    )
            # Auto-call (decision A): first new-DXCC CQ when idle and the
            # backend switch is on; never interrupts a QSO or a manual pick.
            auto_enabled = repository.get_setting("auto_call_new_dxcc") is True
            if auto_enabled and sequencer.state is QSOState.IDLE and state.selected is None:
                for view in views:
                    if auto_call_candidate(
                        view,
                        sequencer_state=sequencer.state.value,
                        has_selection=state.selected is not None,
                        auto_call_enabled=auto_enabled,
                    ):
                        slot_id = slot_decode.slot_id
                        tx_phase = 0 if slot_id is None else 1 - (slot_id % 2)
                        asyncio.get_running_loop().create_task(
                            _auto_call(state, repository, view, slot_id, tx_phase)
                        )
                        break  # first new DXCC in this slot only

            state.decode_broadcast.publish(batch)
            for message in slot_decode.messages:
                asyncio.get_running_loop().create_task(
                    asyncio.to_thread(
                        repository.record_decode_event,
                        slot_id=slot_decode.slot_id,
                        message=message.result.text,
                        snr_db=message.result.snr,
                    )
                )

        orchestrator = Orchestrator(
            supervisor_decoder,
            read_slot_logged,
            sequencer,
            on_decode=on_decode,
            on_decode_error=lambda slot_id, error: report_dsp_fault(
                f"decode slot {slot_id}", error
            ),
            # Per-slot TX tasks are intentionally untracked and uncancelled:
            # the TX lifecycle is owned by the Sequencer (HaltTx / slot
            # timeout), not by task bookkeeping here.
            on_slot_start=lambda slot_id: asyncio.create_task(
                state.tx_driver.on_slot_start(slot_id)
            ),
        )
        state.orchestrator = orchestrator
    else:
        from .engine.tx_driver import TxDriver

        state.tx_driver = TxDriver(sequencer, _NullEncoder(), safety)
        state.tx_driver.on_tx_error = lambda slot_id, error: report_dsp_fault(  # type: ignore[method-assign]
            f"tx slot {slot_id}", error
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        from .engine.qso_log import QsoLog

        main_loop.append(asyncio.get_running_loop())
        qso_log = QsoLog(repository, pending_path=config.pending_path)
        # §12.3: interrupted QSOs, then monitor-only safety with PTT off.
        aborted = await asyncio.to_thread(repository.abort_active_qsos)
        if aborted:
            await asyncio.to_thread(
                repository.record_audit,
                actor="system",
                operation="aborted_restart",
                target=f"{aborted} qso(s)",
                detail="interrupted by restart",
            )
        # UC-005: sequencer fires completed records exactly once via on_qso;
        # the durable queue owns them from there (retry → dead-letter).
        sequencer.on_qso = qso_log.enqueue
        # §7.5: completed QSO records carry the dial frequency + ADIF band
        # from the last rig poll.  Previously left at the default context
        # (freq 0 / no band), which broke ADIF FREQ/BAND on every QSO.
        sequencer.context = lambda: QsoContext(
            freq_hz=state.radio_freq_hz or 0,
            band=band_from_freq_hz(state.radio_freq_hz or 0),
        )
        await asyncio.to_thread(qso_log.recover)
        await safety.start()

        if capture is not None:
            await asyncio.to_thread(capture.start)
        if orchestrator is not None:
            # Pre-fill the DXCC cache so on_decode can mark is_new_dxcc
            # without a full scan per slot (NFR-086/087).
            if state.dxcc_cache is None or repository.dxcc_dirty:
                from .engine.dxcc import dxcc_summary, get_cty_database

                state.dxcc_cache = await asyncio.to_thread(
                    dxcc_summary, repository, get_cty_database()
                )
                repository.dxcc_dirty = False
            await asyncio.to_thread(supervisor.start)
            tasks.append(asyncio.create_task(orchestrator.run()))

        async def lease_watchdog() -> None:
            while True:
                await asyncio.sleep(LEASE_POLL_S)
                try:
                    state.lease.check_expiry()
                    # UC-005: drain at most one completed QSO per tick; the
                    # queue retries then spills to a dead-letter journal.
                    await qso_log.drain_once()
                    if state.cq_loop is not None:
                        state.cq_loop.tick()
                except Exception:
                    log.exception("lease watchdog tick failed")

        async def maintenance() -> None:
            while True:
                await asyncio.sleep(MAINTENANCE_S)
                try:
                    state.auth.sweep_expired()
                    await asyncio.to_thread(repository.enforce_retention)
                except Exception:
                    log.exception("maintenance tick failed")

        tasks.append(asyncio.create_task(lease_watchdog()))
        tasks.append(asyncio.create_task(maintenance()))

        RIG_POLL_S = 5.0

        async def jtdx_sync() -> None:
            """Startup + hourly incremental import of the JTDX ADIF log.

            Never faults the safety controller: a missing file or parse hiccup
            only logs; the hourly loop retries.  Runs on a worker thread so the
            event loop never blocks on the file read/insert.
            """

            if not config.jtdx_log_path:
                return
            from .engine.adif_import import sync_jtdx_log

            try:
                report = await asyncio.to_thread(
                    sync_jtdx_log,
                    repository,
                    config.jtdx_log_path,
                    my_call=config.my_call,
                    my_grid=config.my_grid,
                )
                if report.error:
                    log.warning("jtdx sync skipped: %s", report.error)
            except Exception:
                log.exception("jtdx sync failed")

        async def jtdx_loop() -> None:
            while True:
                await asyncio.sleep(JTDX_SYNC_S)
                await jtdx_sync()

        await jtdx_sync()  # one import at startup (before the hourly loop)
        tasks.append(asyncio.create_task(jtdx_loop()))

        async def rig_poll() -> None:
            # Slow dial-frequency poll feeding the snapshot's radio view;
            # poll failures keep the last value and never fault (§12
            # monitor-only posture for display-only data).
            while True:
                await asyncio.sleep(RIG_POLL_S)
                try:
                    freq_hz = await state.rig.get_frequency()
                    if freq_hz != state.radio_freq_hz:
                        state.radio_freq_hz = freq_hz
                        state.state_broadcast.publish(_snapshot(state, None))
                except Exception:
                    log.debug("rig frequency poll failed", exc_info=True)

        tasks.append(asyncio.create_task(rig_poll()))
        try:
            yield
        finally:
            # §12.3: priority STOP before audio/rig/worker teardown.
            await safety.stop_tx("shutdown")
            if orchestrator is not None:
                orchestrator.stop()
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if capture is not None:
                await asyncio.to_thread(capture.stop)
            if encoder is not None:
                await asyncio.to_thread(encoder.close)
            if supervisor_decoder is not None:
                await asyncio.to_thread(supervisor_decoder.close)
            if supervisor is not None:
                await asyncio.to_thread(supervisor.stop)
            # UC-005: best-effort persist of any queued/journaled QSOs before
            # the canonical store closes.
            await qso_log.flush()
            repository.close()

    app = create_app(state)
    app.router.lifespan_context = lifespan
    import starlette.responses
    _static_dir_v = _static_dir()
    class _NoCacheStaticFiles(StaticFiles):
        async def __call__(self, scope, receive, send):
            async def _send(msg):
                if msg["type"] == "http.response.start":
                    headers = dict(msg.get("headers", []))
                    headers[b"cache-control"] = b"no-cache, no-store, must-revalidate"
                    msg["headers"] = list(headers.items())
                await send(msg)
            return await super().__call__(scope, receive, _send)
    app.mount("/static", _NoCacheStaticFiles(directory=_static_dir_v), name="static")
    return app


def _static_dir() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent / "web" / "static")


def main() -> None:
    """CLI entry point; by default serves Uvicorn on loopback (§12.1).

    ``--hash-password [PASSWORD]`` is the bootstrap tool for
    ``MRRC_FT8_PASSWORD_HASH`` (§12.6): it prints an Argon2id hash and
    exits.  Without a value it prompts via getpass so the password never
    lands in shell history.
    """

    import argparse

    parser = argparse.ArgumentParser(prog="server.main")
    parser.add_argument(
        "--hash-password",
        nargs="?",
        const="",
        default=None,
        metavar="PASSWORD",
        help="print an Argon2id hash for MRRC_FT8_PASSWORD_HASH and exit; "
        "prompts interactively when no value is given",
    )
    args = parser.parse_args()
    if args.hash_password is not None:
        from getpass import getpass

        from .web.auth import hash_password

        password = args.hash_password or getpass("password to hash: ")
        print(hash_password(password))
        return

    config = ServerConfig.from_env()
    app = create_server(config)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
