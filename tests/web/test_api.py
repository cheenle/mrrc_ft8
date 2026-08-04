"""REST control-rule regressions (§10.1, §10.3, NFR-033..039)."""

from __future__ import annotations

import io
import tarfile
import time

import pytest
from fastapi.testclient import TestClient
from test_audio_tx import FakeOutputStream, run
from test_safety import FakeRig

from server.engine.audio_tx import TxPlayer
from server.engine.cq_loop import CqLoopController
from server.engine.repository import QsoStatus, Repository
from server.engine.safety import Interlock, SafetyController
from server.engine.sequencer import QSORecord, Sequencer
from server.web.api import COOKIE_NAME, AppState, create_app
from server.web.auth import AuthService, hash_password
from server.web.lease import LeaseService

PASSWORD = "correct horse battery staple"


class ApiRig(FakeRig):
    def __init__(self) -> None:
        super().__init__()
        self.frequencies: list[int] = []
        self.levels: dict[str, float] = {"ATT": 0.0, "PREAMP": 0.0, "RF": 50.0, "AGC": 30.0}

    async def set_frequency(self, frequency_hz: int) -> None:
        self.frequencies.append(frequency_hz)

    async def get_mode(self) -> tuple[str, int]:
        return self.mode if hasattr(self, "mode") else ("USB", 2400)

    async def set_mode(self, mode: str, passband_hz: int) -> None:
        self.mode = (mode, passband_hz)

    async def get_level(self, level: str) -> float:
        if level not in self.levels:
            from server.engine.rig import RigError
            raise RigError("rig_unsupported", f"no level {level}", rprt=-11)
        return self.levels[level]

    async def set_level(self, level: str, value: float) -> None:
        if level not in self.levels:
            from server.engine.rig import RigError
            raise RigError("rig_unsupported", f"no level {level}", rprt=-11)
        self.levels[level] = value

    async def set_filter_width(self, hz: int) -> None:
        if hz not in (1800, 2400, 3000):
            raise ValueError(f"unsupported filter width: {hz} Hz")
        self.filter_hz = hz

    async def get_filter_width(self) -> int:
        # The true SH-register width; may legitimately disagree with the
        # hamlib-reported passband in self.mode (the FT-710 hamlib bug).
        if hasattr(self, "filter_hz"):
            return self.filter_hz
        _, passband_hz = await self.get_mode()
        return passband_hz


@pytest.fixture()
def rig() -> ApiRig:
    return ApiRig()


@pytest.fixture()
def state(rig: ApiRig) -> AppState:
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    safety = SafetyController(
        rig, TxPlayer(stream_factory=FakeOutputStream), sequencer=sequencer
    )
    loop_audits: list[tuple[str, str]] = []
    cq_loop = CqLoopController(
        sequencer=sequencer,
        arm=safety.arm,
        lease_alive=lambda: True,
        clock=time.monotonic,
        idle_timeout=lambda: 600,
        on_audit=lambda operation, detail: loop_audits.append((operation, detail)),
    )
    return AppState(
        auth=AuthService(hash_password(PASSWORD)),
        lease=LeaseService(),
        safety=safety,
        sequencer=sequencer,
        repository=Repository(":memory:"),
        my_call="M0XX",
        my_grid="IO91",
        rig=rig,
        allowed_hosts=frozenset({"testserver"}),
        cq_loop=cq_loop,
    )


@pytest.fixture()
def client(state: AppState) -> TestClient:
    return TestClient(create_app(state), base_url="https://testserver")


def login(client: TestClient, password: str = PASSWORD) -> str:
    response = client.post("/api/v1/session/login", json={"password": password})
    assert response.status_code == 200, response.text
    return response.cookies[COOKIE_NAME]


def acquire_lease(client: TestClient) -> None:
    response = client.post("/api/v1/lease/acquire")
    assert response.status_code == 200, response.text


def auth_headers(session_id: str) -> dict[str, str]:
    return {"cookie": f"{COOKIE_NAME}={session_id}"}


def test_login_rejects_wrong_password_and_audits(
    client: TestClient, state: AppState
) -> None:
    response = client.post("/api/v1/session/login", json={"password": "wrong"})
    assert response.status_code == 429  # progressive delay kicks in immediately
    assert response.json()["reason"] == "rate_limited"
    audits = state.repository.audit_events()
    assert audits and audits[0]["operation"] == "login_failure"


def test_login_sets_hardened_cookie_and_session_flow(client: TestClient) -> None:
    response = client.post("/api/v1/session/login", json={"password": PASSWORD})
    assert response.status_code == 200
    header = response.headers["set-cookie"]
    assert "Secure" in header and "HttpOnly" in header
    assert "samesite=strict" in header.lower()
    session_id = response.cookies[COOKIE_NAME]

    current = client.get("/api/v1/session/current", headers=auth_headers(session_id))
    assert current.status_code == 200
    assert current.json()["ok"]

    client.post("/api/v1/session/logout", headers=auth_headers(session_id))
    gone = client.get("/api/v1/session/current", headers=auth_headers(session_id))
    assert gone.status_code == 401
    assert gone.json()["reason"] == "unauthenticated"


def test_reads_require_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/state").status_code == 401
    assert client.get("/api/v1/logs/qsos").status_code == 401


def test_root_redirects_to_the_pwa(client: TestClient) -> None:
    """Mobile browsers open /; the PWA lives under /static (deployment UX)."""

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/static/index.html"


def test_mutations_validate_host_and_origin(client: TestClient) -> None:
    session_id = login(client)
    bad_host = client.post(
        "/api/v1/operation/stop",
        headers={"host": "evil.example.com", **auth_headers(session_id)},
    )
    assert bad_host.status_code == 403
    bad_origin = client.post(
        "/api/v1/operation/stop",
        headers={"origin": "https://evil.example.com", **auth_headers(session_id)},
    )
    assert bad_origin.status_code == 403


def test_lease_arbitration_between_sessions(client: TestClient) -> None:
    first, second = login(client), login(client)
    mine = client.post("/api/v1/lease/acquire", headers=auth_headers(first))
    assert mine.status_code == 200 and mine.json()["lease"] == "held"
    theirs = client.post("/api/v1/lease/acquire", headers=auth_headers(second))
    assert theirs.status_code == 409
    assert theirs.json()["reason"] == "lease_required"

    view = client.get("/api/v1/lease", headers=auth_headers(second)).json()
    assert view["held"] and not view["mine"]
    beat = client.post("/api/v1/lease/heartbeat", headers=auth_headers(first))
    assert beat.status_code == 200
    client.post("/api/v1/lease/release", headers=auth_headers(first))
    now = client.post("/api/v1/lease/acquire", headers=auth_headers(second))
    assert now.status_code == 200


def test_stale_revision_is_rejected(client: TestClient, state: AppState) -> None:
    session_id = login(client)
    stale = client.post(
        "/api/v1/operation/stop",
        headers={"x-expected-revision": "99", **auth_headers(session_id)},
    )
    assert stale.status_code == 409
    assert stale.json()["reason"] == "stale_revision"
    current = stale.json()["revision"]
    fresh = client.post(
        "/api/v1/operation/stop",
        headers={"x-expected-revision": str(current), **auth_headers(session_id)},
    )
    assert fresh.status_code == 200


def test_idempotency_key_replays_without_side_effects(
    client: TestClient, state: AppState
) -> None:
    session_id = login(client)
    headers = {"idempotency-key": "stop-1", **auth_headers(session_id)}
    first = client.post("/api/v1/operation/stop", headers=headers)
    replay = client.post("/api/v1/operation/stop", headers=headers)
    assert first.status_code == 200 and replay.status_code == 200
    assert replay.headers.get("x-idempotent-replay") == "true"
    assert replay.json()["revision"] == first.json()["revision"]
    assert state.revision == first.json()["revision"]  # no second bump


def test_select_then_reply_arms_and_audits(
    client: TestClient, state: AppState
) -> None:
    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))
    reply_early = client.post("/api/v1/operation/reply", headers=auth_headers(session_id))
    assert reply_early.status_code == 409
    assert reply_early.json()["reason"] == "no_selection"

    select = client.post(
        "/api/v1/operation/select",
        json={"dx_call": "k1abc", "dx_grid": "fn42", "snr_db": -15, "is_cq": True},
        headers=auth_headers(session_id),
    )
    assert select.status_code == 200
    assert select.json()["selected"] == "K1ABC"
    assert not state.sequencer.tx_enabled  # selection alone never arms (§15.6)

    reply = client.post("/api/v1/operation/reply", headers=auth_headers(session_id))
    assert reply.status_code == 200
    assert state.sequencer.tx_enabled
    assert state.sequencer.dx_call == "K1ABC"
    assert state.safety.armed
    operations = [a["operation"] for a in state.repository.audit_events()]
    assert "reply" in operations


def test_reply_phase_is_opposite_the_selected_slot(
    client: TestClient, state: AppState
) -> None:
    """UC-003: replying to a message heard in an even slot arms odd-phase TX."""

    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))

    client.post(
        "/api/v1/operation/select",
        json={"dx_call": "K1ABC", "dx_grid": "FN42", "snr_db": -15, "slot_id": 0},
        headers=auth_headers(session_id),
    )
    client.post("/api/v1/operation/reply", headers=auth_headers(session_id))
    assert state.sequencer.tx_phase == 1  # even slot -> reply on odd

    # Selecting a fresh target without a slot keeps the default even phase.
    client.post(
        "/api/v1/operation/select",
        json={"dx_call": "W9XYZ", "snr_db": -8, "slot_id": 1},
        headers=auth_headers(session_id),
    )
    client.post("/api/v1/operation/reply", headers=auth_headers(session_id))
    assert state.sequencer.tx_phase == 0  # odd slot -> reply on even


def test_cq_and_tx_off_require_the_lease(client: TestClient, state: AppState) -> None:
    session_id = login(client)
    no_lease = client.post("/api/v1/operation/cq", headers=auth_headers(session_id))
    assert no_lease.status_code == 409
    assert no_lease.json()["reason"] == "lease_required"

    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))
    cq = client.post("/api/v1/operation/cq", headers=auth_headers(session_id))
    assert cq.status_code == 200
    assert state.sequencer.tx_enabled
    off = client.post("/api/v1/operation/enable_tx_off", headers=auth_headers(session_id))
    assert off.status_code == 200
    assert not state.sequencer.tx_enabled
    assert not state.safety.armed


def test_observer_stop_bypasses_the_lease(client: TestClient, state: AppState) -> None:
    operator, observer = login(client), login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(operator))
    client.post("/api/v1/operation/cq", headers=auth_headers(operator))
    assert state.sequencer.tx_enabled

    stop = client.post("/api/v1/operation/stop", headers=auth_headers(observer))
    assert stop.status_code == 200  # NFR-038: no lease needed
    assert not state.sequencer.tx_enabled
    assert not state.safety.armed
    stop_audits = [a for a in state.repository.audit_events() if a["operation"] == "stop"]
    assert stop_audits and stop_audits[0]["actor"].startswith("session-")


def test_clear_fault_requires_the_lease(client: TestClient) -> None:
    session_id = login(client)
    response = client.post("/api/v1/operation/clear-fault", headers=auth_headers(session_id))
    assert response.status_code == 409
    assert response.json()["reason"] == "lease_required"


def test_clear_fault_restores_arm_after_latched_dsp_fault(
    client: TestClient, state: AppState
) -> None:
    """A transient DSP fault latches TX off; the lease holder can clear it
    and re-arm without restarting the process (§15.5 recovery path)."""
    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))
    run(state.safety.report_fault(Interlock.DSP, "worker died"))

    refused = client.post("/api/v1/operation/cq", headers=auth_headers(session_id))
    assert refused.status_code == 409
    assert refused.json()["reason"] == "interlock_open"

    cleared = client.post(
        "/api/v1/operation/clear-fault",
        json={"interlock": "dsp"},
        headers=auth_headers(session_id),
    )
    assert cleared.status_code == 200
    assert cleared.json()["safety"]["faults"] == []
    assert not state.safety.faults
    audits = [a for a in state.repository.audit_events() if a["operation"] == "clear_fault"]
    assert audits and "dsp" in audits[0]["detail"]

    cq = client.post("/api/v1/operation/cq", headers=auth_headers(session_id))
    assert cq.status_code == 200
    assert state.safety.armed


def test_clear_fault_rejects_unknown_interlock(client: TestClient) -> None:
    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))
    response = client.post(
        "/api/v1/operation/clear-fault",
        json={"interlock": "flux-capacitor"},
        headers=auth_headers(session_id),
    )
    assert response.status_code == 422


def test_clear_fault_without_body_clears_all_faults(
    client: TestClient, state: AppState
) -> None:
    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))
    run(state.safety.report_fault(Interlock.DSP, "worker died"))
    run(state.safety.report_fault(Interlock.CLOCK, "ntp lost"))
    assert state.safety.faults == {Interlock.DSP, Interlock.CLOCK}

    response = client.post("/api/v1/operation/clear-fault", headers=auth_headers(session_id))
    assert response.status_code == 200
    assert response.json()["safety"]["faults"] == []
    assert not state.safety.faults
    audits = [a for a in state.repository.audit_events() if a["operation"] == "clear_fault"]
    assert audits and "all" in audits[0]["detail"]


def test_radio_band_rules(client: TestClient, state: AppState, rig: ApiRig) -> None:
    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))
    bad = client.post(
        "/api/v1/radio/band", json={"freq_hz": 50}, headers=auth_headers(session_id)
    )
    assert bad.status_code == 422
    ok = client.post(
        "/api/v1/radio/band",
        json={"freq_hz": 14_074_000},
        headers=auth_headers(session_id),
    )
    assert ok.status_code == 200
    assert rig.frequencies == [14_074_000]

    client.post("/api/v1/operation/cq", headers=auth_headers(session_id))
    during_tx = client.post(
        "/api/v1/radio/band",
        json={"freq_hz": 7_074_000},
        headers=auth_headers(session_id),
    )
    assert during_tx.status_code == 409
    assert during_tx.json()["reason"] == "tx_active"

    state.rig = None
    client.post("/api/v1/operation/enable_tx_off", headers=auth_headers(session_id))
    gone = client.post(
        "/api/v1/radio/band",
        json={"freq_hz": 7_074_000},
        headers=auth_headers(session_id),
    )
    assert gone.status_code == 503


def test_radio_rig_levels_and_station_snapshot(
    client: TestClient, state: AppState
) -> None:
    """Rig level read/write endpoints and the snapshot's station block."""

    session_id = login(client)
    headers = auth_headers(session_id)
    client.post("/api/v1/lease/acquire", headers=headers)

    levels = client.get("/api/v1/radio/rig/levels", headers=headers)
    assert levels.status_code == 200
    assert levels.json()["levels"] == {"ATT": 0.0, "PREAMP": 0.0, "RF": 50.0, "AGC": 30.0}

    set_att = client.post(
        "/api/v1/radio/rig/level", json={"level": "ATT", "value": 1}, headers=headers
    )
    assert set_att.status_code == 200
    assert set_att.json()["value"] == 1

    # A level write drops the 60 s capability snapshot: the next read must
    # re-probe the rig and report the new value, not the stale cached one.
    levels2 = client.get("/api/v1/radio/rig/levels", headers=headers)
    assert levels2.status_code == 200
    assert levels2.json()["levels"]["ATT"] == 1.0

    bad = client.post(
        "/api/v1/radio/rig/level", json={"level": "att; DROP", "value": 1}, headers=headers
    )
    assert bad.status_code == 422

    snap = client.get("/api/v1/state", headers=headers)
    assert snap.status_code == 200
    station = snap.json()["station"]
    assert station["my_call"] == state.my_call
    assert isinstance(station["worked_calls"], list)

    # Filter bandwidth: read current, then set USB 1.8 kHz.
    mode = client.get("/api/v1/radio/mode", headers=headers)
    assert mode.status_code == 200
    assert mode.json()["passband_hz"] == 2400
    set_mode = client.post(
        "/api/v1/radio/mode",
        json={"mode": "USB", "passband_hz": 1800},
        headers=headers,
    )
    assert set_mode.status_code == 200
    assert set_mode.json()["passband_hz"] == 1800

    bad_mode = client.post(
        "/api/v1/radio/mode",
        json={"mode": "usb; drop", "passband_hz": 2400},
        headers=headers,
    )
    assert bad_mode.status_code == 422


def test_radio_filter_sets_width_and_readback_follows_the_rig(
    client: TestClient, state: AppState
) -> None:
    """POST /radio/filter writes the FT-710 width; GET /radio/mode must then
    report the rig's true width, not hamlib's (which misreads 2400 as 1800
    on hamlib 4.6.2)."""

    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))

    set_filter = client.post(
        "/api/v1/radio/filter", json={"hz": 2400}, headers=auth_headers(session_id)
    )
    assert set_filter.status_code == 200
    assert set_filter.json()["filter_hz"] == 2400
    assert state.rig.filter_hz == 2400  # type: ignore[union-attr]

    mode = client.get("/api/v1/radio/mode", headers=auth_headers(session_id))
    assert mode.status_code == 200
    assert mode.json()["passband_hz"] == 2400


def test_radio_mode_readback_prefers_true_rig_width_over_hamlib(
    client: TestClient, state: AppState
) -> None:
    """Simulates the hamlib 4.6.2 misread: ``m`` claims 1800 while the rig's
    SH register is at 2400 — the drawer must show 2400."""

    rig = state.rig
    assert rig is not None
    rig.mode = ("USB", 1800)  # what hamlib's broken GET reports
    rig.filter_hz = 2400      # what the rig is actually set to

    session_id = login(client)
    mode = client.get("/api/v1/radio/mode", headers=auth_headers(session_id))
    assert mode.status_code == 200
    assert mode.json()["mode"] == "USB"
    assert mode.json()["passband_hz"] == 2400


def test_radio_mode_set_also_applies_the_width(
    client: TestClient, state: AppState
) -> None:
    """POST /radio/mode must not silently drop the width on hamlib 4.6.2."""

    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))

    set_mode = client.post(
        "/api/v1/radio/mode",
        json={"mode": "USB", "passband_hz": 3000},
        headers=auth_headers(session_id),
    )
    assert set_mode.status_code == 200
    assert state.rig.filter_hz == 3000  # type: ignore[union-attr]


def test_radio_filter_validation_and_control_rules(
    client: TestClient, state: AppState
) -> None:
    session_id = login(client)

    # No lease -> 409.
    no_lease = client.post(
        "/api/v1/radio/filter", json={"hz": 2400}, headers=auth_headers(session_id)
    )
    assert no_lease.status_code == 409
    assert no_lease.json()["reason"] == "lease_required"

    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))

    # Unsupported width -> 422.
    bad = client.post(
        "/api/v1/radio/filter", json={"hz": 2300}, headers=auth_headers(session_id)
    )
    assert bad.status_code == 422

    # TX armed -> 409 tx_active.
    run(state.safety.arm())
    blocked = client.post(
        "/api/v1/radio/filter", json={"hz": 2400}, headers=auth_headers(session_id)
    )
    assert blocked.status_code == 409
    assert blocked.json()["reason"] == "tx_active"
    state.safety.disarm()

    # Rig down -> 503.
    state.rig = None
    gone = client.post(
        "/api/v1/radio/filter", json={"hz": 2400}, headers=auth_headers(session_id)
    )
    assert gone.status_code == 503


def test_qso_listing_and_audited_void(client: TestClient, state: AppState) -> None:
    record = QSORecord(
        my_call="M0XX", my_grid="IO91", dx_call="K1ABC", dx_grid="FN42",
        report_sent=-12, report_rcvd=-8, started_utc="120000",
        freq_hz=14_074_000, band="20m",
    )
    qso_id = state.repository.record_qso(record)
    session_id = login(client)

    listing = client.get("/api/v1/logs/qsos", headers=auth_headers(session_id)).json()
    # The drawer gates on ``res.ok``; the _ok envelope is part of the contract.
    assert listing.get("ok") is True
    assert listing["qsos"][0]["dx_call"] == "K1ABC"

    voided = client.post(
        f"/api/v1/logs/qsos/{qso_id}/void",
        json={"reason": "wrong grid"},
        headers=auth_headers(session_id),
    )
    assert voided.status_code == 200
    assert state.repository.get_qso(qso_id).status is QsoStatus.VOID  # type: ignore[union-attr]
    again = client.post(
        f"/api/v1/logs/qsos/{qso_id}/void",
        json={"reason": "twice"},
        headers=auth_headers(session_id),
    )
    assert again.status_code == 409
    missing = client.post(
        "/api/v1/logs/qsos/999/void",
        json={"reason": "-"},
        headers=auth_headers(session_id),
    )
    assert missing.status_code == 404


def test_adif_export_lists_only_non_void(client: TestClient, state: AppState) -> None:
    record = QSORecord(
        my_call="M0XX", my_grid="IO91", dx_call="K1ABC", dx_grid="FN42",
        report_sent=-12, report_rcvd=-8, started_utc="120000",
        freq_hz=14_074_000, band="20m",
    )
    state.repository.record_qso(record)
    session_id = login(client)
    response = client.get("/api/v1/logs/adif", headers=auth_headers(session_id))
    assert response.status_code == 200
    assert "<CALL:5>K1ABC" in response.text
    assert "attachment" in response.headers["content-disposition"]


def test_diagnostic_export_requires_reauth(client: TestClient, state: AppState) -> None:
    session_id = login(client)
    denied = client.post("/api/v1/diagnostics/export", headers=auth_headers(session_id))
    assert denied.status_code == 403
    assert denied.json()["reason"] == "reauth_required"

    bad = client.post(
        "/api/v1/session/reauth",
        json={"password": "nope"},
        headers=auth_headers(session_id),
    )
    assert bad.status_code == 403
    ok = client.post(
        "/api/v1/session/reauth",
        json={"password": PASSWORD},
        headers=auth_headers(session_id),
    )
    assert ok.status_code == 200

    bundle = client.post("/api/v1/diagnostics/export", headers=auth_headers(session_id))
    assert bundle.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(bundle.content), mode="r:gz") as archive:
        names = archive.getnames()
        health = archive.extractfile("health.json").read().decode()  # type: ignore[union-attr]
    assert {"health.json", "settings.json", "qsos.json", "audit.json"} <= set(names)
    assert PASSWORD not in bundle.content.decode("latin-1")  # secrets never exported
    assert session_id not in bundle.content.decode("latin-1")
    assert "safety" in health
    operations = [a["operation"] for a in state.repository.audit_events()]
    assert "diagnostic_export" in operations


def test_settings_validation_and_tx_lock(client: TestClient, state: AppState) -> None:
    session_id = login(client)
    defaults = client.get("/api/v1/settings", headers=auth_headers(session_id)).json()
    assert defaults["settings"]["decoder_profile"] is None

    invalid = client.put(
        "/api/v1/settings", json={"decoder_profile": 9}, headers=auth_headers(session_id)
    )
    assert invalid.status_code == 422
    unknown = client.put(
        "/api/v1/settings", json={"nonsense": 1}, headers=auth_headers(session_id)
    )
    assert unknown.status_code == 422

    updated = client.put(
        "/api/v1/settings",
        json={"decoder_profile": 3, "waterfall_lines_per_second": 4.0},
        headers=auth_headers(session_id),
    )
    assert updated.status_code == 200
    assert state.repository.get_setting("decoder_profile") == 3

    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))
    client.post("/api/v1/operation/cq", headers=auth_headers(session_id))
    locked = client.put(
        "/api/v1/settings", json={"decoder_threads": 7}, headers=auth_headers(session_id)
    )
    assert locked.status_code == 409
    assert locked.json()["reason"] == "tx_active"
    cosmetic = client.put(
        "/api/v1/settings",
        json={"waterfall_lines_per_second": 3.0},
        headers=auth_headers(session_id),
    )
    assert cosmetic.status_code == 200  # not safety-impacting


def test_state_snapshot_shape(client: TestClient, state: AppState) -> None:
    session_id = login(client)
    snapshot = client.get("/api/v1/state", headers=auth_headers(session_id)).json()
    # boot() gates on ``snapshot.ok``; without the envelope the client never
    # applies the initial snapshot (worked_calls drives hide-already-worked).
    assert snapshot.get("ok") is True
    assert snapshot["revision"] == state.revision
    assert snapshot["lease"]["held"] is False
    assert snapshot["safety"]["armed"] is False
    assert snapshot["sequencer"]["state"] == "idle"


def test_state_snapshot_marks_lease_mine_for_the_holder(client: TestClient) -> None:
    """REST /state keeps its per-session lease view (boot path, §11.3)."""
    holder, observer = login(client), login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(holder))
    mine = client.get("/api/v1/state", headers=auth_headers(holder)).json()
    theirs = client.get("/api/v1/state", headers=auth_headers(observer)).json()
    assert mine["lease"] == {"held": True, "mine": True}
    assert theirs["lease"] == {"held": True, "mine": False}


def test_state_snapshot_selected_is_a_call_grid_object(client: TestClient) -> None:
    """The snapshot selected shape matches what candidates.js patches
    ({call, grid}) so selection highlighting survives state pushes."""
    session_id = login(client)
    client.post("/api/v1/lease/acquire", headers=auth_headers(session_id))
    assert client.get("/api/v1/state", headers=auth_headers(session_id)).json()["selected"] is None
    client.post(
        "/api/v1/operation/select",
        json={"dx_call": "k1abc", "dx_grid": "fn42", "snr_db": -15},
        headers=auth_headers(session_id),
    )
    snapshot = client.get("/api/v1/state", headers=auth_headers(session_id)).json()
    assert snapshot["selected"] == {"call": "K1ABC", "grid": "FN42"}


def test_cq_loop_requires_lease(client: TestClient) -> None:
    login(client)
    response = client.post("/api/v1/operation/cq", json={"loop": True})
    assert response.status_code == 409  # observer without lease


def test_cq_loop_start_and_snapshot(client: TestClient) -> None:
    login(client)
    acquire_lease(client)
    response = client.post("/api/v1/operation/cq", json={"loop": True})
    assert response.status_code == 200
    snapshot = client.get("/api/v1/state").json()
    assert snapshot["sequencer"]["cq_loop"]["active"] is True
    assert snapshot["sequencer"]["cq_loop"]["idle_remaining_s"] > 0


def test_cq_without_loop_keeps_legacy_behavior(client: TestClient) -> None:
    login(client)
    acquire_lease(client)
    response = client.post(
        "/api/v1/operation/cq", headers={"idempotency-key": "k1"}
    )
    assert response.status_code == 200
    snapshot = client.get("/api/v1/state").json()
    assert snapshot["sequencer"]["cq_loop"]["active"] is False


def test_cq_loop_timeout_setting_bounds(client: TestClient) -> None:
    login(client)
    assert client.put(
        "/api/v1/settings", json={"cq_loop_idle_timeout_s": 59}
    ).status_code == 422
    assert client.put(
        "/api/v1/settings", json={"cq_loop_idle_timeout_s": 3601}
    ).status_code == 422
    assert client.put(
        "/api/v1/settings", json={"cq_loop_idle_timeout_s": 300}
    ).status_code == 200


def test_logs_qsos_windows_to_recent_week(client: TestClient, state: AppState) -> None:
    now = time.time()
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="OLD1"),
        completed_epoch=now - 8 * 86_400,
    )
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="FRESH1"),
        completed_epoch=now,
    )
    session_id = login(client)
    body = client.get(
        "/api/v1/logs/qsos", headers=auth_headers(session_id)
    ).json()
    assert [q["dx_call"] for q in body["qsos"]] == ["FRESH1"]


def test_adif_export_windows_to_recent_week(client: TestClient, state: AppState) -> None:
    now = time.time()
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="OLD1"),
        completed_epoch=now - 8 * 86_400,
    )
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="FRESH1"),
        completed_epoch=now,
    )
    session_id = login(client)
    text = client.get(
        "/api/v1/logs/adif", headers=auth_headers(session_id)
    ).text
    assert "FRESH1" in text
    assert "OLD1" not in text
