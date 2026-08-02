"""Lifespan composition regressions (§12.3 startup/shutdown ordering)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_safety import FakeRig

from server.engine.msgparse import parse_message
from server.engine.repository import QsoStatus, Repository
from server.engine.safety import Interlock, SafetyEventKind
from server.engine.sequencer import DisarmReason, QSORecord, QSOState
from server.main import ServerConfig, create_server
from server.web.auth import hash_password

PASSWORD = "correct horse battery staple"


def make_config(db_path: str = ":memory:") -> ServerConfig:
    return ServerConfig(
        password_hash=hash_password(PASSWORD),
        my_call="M0XX",
        my_grid="IO91",
        allowed_hosts=frozenset({"testserver"}),
        db_path=db_path,
    )


def test_from_env_requires_safety_impacting_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MRRC_FT8_PASSWORD_HASH", "MRRC_FT8_MY_CALL", "MRRC_FT8_MY_GRID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="MRRC_FT8_PASSWORD_HASH"):
        ServerConfig.from_env()

    monkeypatch.setenv("MRRC_FT8_PASSWORD_HASH", "hash")
    monkeypatch.setenv("MRRC_FT8_MY_CALL", "m0xx")
    monkeypatch.setenv("MRRC_FT8_MY_GRID", "io91")
    monkeypatch.setenv("MRRC_FT8_ALLOWED_HOSTS", "ft8.example.com, example.org")
    monkeypatch.setenv("MRRC_FT8_RIGCTLD", "10.0.0.2:4600")
    config = ServerConfig.from_env()
    assert config.my_call == "M0XX"
    assert config.allowed_hosts == frozenset({"ft8.example.com", "example.org"})
    assert config.rigctld_host == "10.0.0.2"
    assert config.rigctld_port == 4600


def test_from_env_optional_devices_and_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """§12.6: audio device and decoder profile/threads are configurable."""

    monkeypatch.setenv("MRRC_FT8_PASSWORD_HASH", "hash")
    monkeypatch.setenv("MRRC_FT8_MY_CALL", "M0XX")
    monkeypatch.setenv("MRRC_FT8_MY_GRID", "IO91")
    monkeypatch.delenv("MRRC_FT8_AUDIO_DEVICE", raising=False)
    monkeypatch.delenv("MRRC_FT8_DECODER_PROFILE", raising=False)
    monkeypatch.delenv("MRRC_FT8_DECODER_THREADS", raising=False)
    config = ServerConfig.from_env()
    assert config.audio_device is None
    assert config.decoder_profile == 3
    assert config.decoder_threads == 0  # Auto (I9)

    monkeypatch.setenv("MRRC_FT8_AUDIO_DEVICE", "4")
    assert ServerConfig.from_env().audio_device == 4
    monkeypatch.setenv("MRRC_FT8_AUDIO_DEVICE", "USB Audio Device")
    assert ServerConfig.from_env().audio_device == "USB Audio Device"
    monkeypatch.setenv("MRRC_FT8_DECODER_PROFILE", "4")
    monkeypatch.setenv("MRRC_FT8_DECODER_THREADS", "8")
    config = ServerConfig.from_env()
    assert (config.decoder_profile, config.decoder_threads) == (4, 8)

    monkeypatch.setenv("MRRC_FT8_DECODER_PROFILE", "5")
    with pytest.raises(ValueError, match="0..4"):
        ServerConfig.from_env()
    monkeypatch.setenv("MRRC_FT8_DECODER_PROFILE", "3")
    monkeypatch.setenv("MRRC_FT8_DECODER_THREADS", "13")
    with pytest.raises(ValueError, match="1..12"):
        ServerConfig.from_env()
    monkeypatch.setenv("MRRC_FT8_DECODER_THREADS", "many")
    with pytest.raises(ValueError, match="auto"):
        ServerConfig.from_env()


class FakeSlotMessage:
    class result:
        text = "M0XX K1ABC FN42"
        snr = -12
        dt = 0.12
        frequency = 1234.5

    class parsed:
        from_call = "K1ABC"
        grid = "FN42"
        is_cq = False
        to_call = "M0XX"


def test_decode_message_view_carries_band_activity_fields() -> None:
    from server.main import decode_message_view

    item = decode_message_view(FakeSlotMessage(), "M0XX")
    assert item["dt"] == 0.12
    assert item["freq"] == 1234.5
    assert item["to_me"] is True
    assert item["call"] == "K1ABC"
    assert item["is_cq"] is False


def test_decode_message_view_empty_my_call_is_not_addressed() -> None:
    from server.main import decode_message_view

    assert decode_message_view(FakeSlotMessage(), "")["to_me"] is False


def test_startup_is_monitor_only_with_ptt_off() -> None:
    rig = FakeRig()
    app = create_server(make_config(), rig=rig, start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver"):
        pass  # lifespan startup ran
    assert rig.calls[0] is False  # best-effort PTT-off during startup (NFR-058)


def test_interrupted_qso_becomes_aborted_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "qso.db")
    seed = Repository(db)
    qso_id = seed.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="K1ABC"),
        status=QsoStatus.ACTIVE,
    )
    seed.close()

    rig = FakeRig()
    app = create_server(make_config(db), rig=rig, start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver"):
        check = Repository(db)
        assert check.get_qso(qso_id).status is QsoStatus.ABORTED_RESTART  # type: ignore[union-attr]
        operations = [a["operation"] for a in check.audit_events()]
        assert "aborted_restart" in operations
        check.close()


def test_dead_man_wiring_reaches_the_safety_controller() -> None:
    rig = FakeRig()
    app = create_server(make_config(), rig=rig, start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/api/v1/session/login", json={"password": PASSWORD})
        session_id = response.cookies["mrrc_session"]
        client.post("/api/v1/lease/acquire", headers={"cookie": f"mrrc_session={session_id}"})
        state = app.state.app_state
        assert state.lease.is_owner(session_id)
        # TTL expiry/disconnect must schedule a priority STOP (§15.4)
        state.lease.disconnect(session_id)
        time.sleep(0.1)  # let the scheduled stop_tx task run
        assert state.safety.stop_count >= 1
        assert rig.calls[-1] is False


def test_shutdown_stops_tx_before_teardown() -> None:
    rig = FakeRig()
    app = create_server(make_config(), rig=rig, start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver"):
        pass
    state = app.state.app_state
    assert state.safety.stop_count >= 1  # priority STOP ran during shutdown
    assert rig.calls[-1] is False  # final command was PTT-off


def test_static_shell_is_served() -> None:
    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/static/index.html")
        assert response.status_code == 200
        assert "MRRC-FT8" in response.text
        manifest = client.get("/static/manifest.webmanifest")
        assert manifest.status_code == 200


def test_composition_wires_tx_driver() -> None:
    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app):
        state = app.state.app_state
        assert state.tx_driver is not None
        assert state.tx_driver.counters == {"tx_attempts": 0, "tx_failed": 0}
        assert state.tx_driver.sequencer is state.sequencer


def test_composition_wires_cq_loop() -> None:
    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app):
        state = app.state.app_state
        assert state.cq_loop is not None
        assert state.cq_loop.status() == {"active": False, "idle_remaining_s": 0}


def test_composition_records_completed_qso() -> None:
    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app):
        state = app.state.app_state
        sequencer = state.sequencer
        sequencer.start_cq()
        sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
        sequencer.on_message(parse_message("M0XX K1ABC R-10"), snr_db=-10)
        sequencer.on_message(parse_message("M0XX K1ABC RR73"), snr_db=-9)
        sequencer.next_tx_message()
        sequencer.next_tx_message()  # DONE
        # Poll until the watchdog poll (1 s) records the QSO; bounded to 3 s.
        qsos = state.repository.list_qsos()
        deadline = time.monotonic() + 3.0
        while not qsos and time.monotonic() < deadline:
            time.sleep(0.05)
            qsos = state.repository.list_qsos()
        assert [q.dx_call for q in qsos] == ["K1ABC"]


def test_lease_release_disarms_sequencer_and_safety() -> None:
    """I1: a deliberate release retracts armed TX, not just the lease."""

    rig = FakeRig()
    app = create_server(make_config(), rig=rig, start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver") as client:
        state = app.state.app_state
        response = client.post("/api/v1/session/login", json={"password": PASSWORD})
        session_id = response.cookies["mrrc_session"]
        client.post("/api/v1/lease/acquire", headers={"cookie": f"mrrc_session={session_id}"})
        state.sequencer.start_cq()
        asyncio.run(state.safety.arm())
        assert state.sequencer.state is QSOState.CALLING
        assert state.safety.armed
        stops_before = state.safety.stop_count
        state.lease.release(session_id)
        assert state.sequencer.state is QSOState.IDLE
        assert state.sequencer.disarm_reason is DisarmReason.MANUAL
        assert not state.safety.armed
        # A deliberate release is not the dead-man path: no priority STOP.
        assert state.safety.stop_count == stops_before


def test_composition_reports_tx_errors_as_dsp_fault() -> None:
    """I2: TxDriver encode/transmit failures latch a DSP interlock fault."""

    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver"):
        state = app.state.app_state
        state.tx_driver.on_tx_error(0, RuntimeError("worker gone"))
        deadline = time.monotonic() + 3.0
        while Interlock.DSP not in state.safety.faults and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Interlock.DSP in state.safety.faults
        assert not state.safety.armed
        # The fault latches: a dead worker erroring every slot repeats nothing.
        faults_seen = sum(
            e.kind is SafetyEventKind.FAULT for e in state.safety.events
        )
        state.tx_driver.on_tx_error(2, RuntimeError("still gone"))
        time.sleep(0.2)
        assert (
            sum(e.kind is SafetyEventKind.FAULT for e in state.safety.events)
            == faults_seen
        )


def test_composition_reports_decode_errors_as_dsp_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I2: Orchestrator decode failures reach safety.report_fault(DSP)."""

    from server.core.supervisor import WorkerSupervisor

    monkeypatch.setattr(WorkerSupervisor, "start", lambda self: None)
    monkeypatch.setattr(WorkerSupervisor, "stop", lambda self: None)
    app = create_server(make_config(), rig=FakeRig(), start_dsp=True, start_audio=False)
    with TestClient(app, base_url="https://testserver"):
        state = app.state.app_state
        orchestrator = state.orchestrator
        assert orchestrator is not None
        on_decode_error = orchestrator._on_decode_error
        assert on_decode_error is not None  # wired at the composition root
        on_decode_error(0, RuntimeError("worker gone"))
        deadline = time.monotonic() + 3.0
        while Interlock.DSP not in state.safety.faults and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Interlock.DSP in state.safety.faults


def test_maintenance_survives_retention_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """M5: a failing retention pass must not kill the maintenance loop."""

    monkeypatch.setattr("server.main.MAINTENANCE_S", 0.05)
    app = create_server(make_config(), rig=FakeRig(), start_dsp=False, start_audio=False)
    with TestClient(app, base_url="https://testserver"):
        state = app.state.app_state
        sweeps = 0

        def counting_sweep() -> None:
            nonlocal sweeps
            sweeps += 1

        def boom() -> None:
            raise RuntimeError("retention failed")

        monkeypatch.setattr(state.auth, "sweep_expired", counting_sweep)
        monkeypatch.setattr(state.repository, "enforce_retention", boom)
        deadline = time.monotonic() + 2.0
        while sweeps < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sweeps >= 2  # the loop kept ticking after the failure
