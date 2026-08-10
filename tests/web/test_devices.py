"""Station device config REST endpoints — TDD task 3 (spec 2026-08-10)."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import PASSWORD, ApiRig, acquire_lease, auth_headers, login

from server.engine.audio_tx import TxPlayer
from server.engine.device_config import DeviceConfigStore, save_device_config
from server.engine.repository import Repository
from server.engine.safety import SafetyController
from server.engine.sequencer import Sequencer
from server.web.api import AppState, create_app
from server.web.auth import AuthService, hash_password
from server.web.lease import LeaseService


def build_state(tmp_path, rig: ApiRig) -> AppState:
    from test_audio_tx import FakeOutputStream

    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    safety = SafetyController(
        rig, TxPlayer(stream_factory=FakeOutputStream), sequencer=sequencer
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
        device_config=DeviceConfigStore(tmp_path / "device-config.json"),
    )


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(build_state(tmp_path, ApiRig())), base_url="https://testserver")


def test_devices_view_reports_config_source_and_enums(client, monkeypatch) -> None:
    session_id = login(client)
    # Deterministic: effective_config/source_of read the real os.environ.
    monkeypatch.delenv("MRRC_FT8_RIGCTLD", raising=False)
    monkeypatch.delenv("MRRC_FT8_AUDIO_DEVICE", raising=False)
    monkeypatch.delenv("MRRC_FT8_RIG_MODEL", raising=False)
    fake_sd = SimpleNamespace(query_devices=lambda: [
        {"name": "USB Audio", "max_input_channels": 2, "max_output_channels": 2},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(
        "server.engine.device_config.glob.glob",
        lambda pat: ["/dev/cu.usbserial-0121DB3A0"] if "cu." in pat else [],
    )
    res = client.get("/api/v1/devices", headers=auth_headers(session_id))
    assert res.status_code == 200
    body = res.json()
    assert body["config"]["rigctld_port"] == 4532
    assert body["audio_devices"][0]["name"] == "USB Audio"
    assert body["serial_devices"] == ["/dev/cu.usbserial-0121DB3A0"]
    assert body["rig_status"]["port"] == 4532
    assert body["ok"] is True  # PWA api.js relies on the payload ok flag
    assert body["curated_rig_models"][0]["model"] == 1020
    assert body["baud_rates"] == [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
    assert body["source"]["audio_device"] == "default"


def test_devices_put_saves_file(client, tmp_path, monkeypatch) -> None:
    session_id = login(client)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda: [
        {"name": "USB Audio", "max_input_channels": 2, "max_output_channels": 2},
    ]))
    res = client.put(
        "/api/v1/devices",
        json={"rig_model": 1049, "rig_device": "/dev/cu.x", "rig_baud": 38400,
              "rigctld_port": 4532, "audio_device": "USB Audio"},
        headers=auth_headers(session_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["saved"] is True
    assert (tmp_path / "device-config.json").exists()


def test_devices_put_allows_empty_rig_device_as_unset(client, tmp_path, monkeypatch) -> None:
    """UI 表单总是提交 rig_device（本站串口在 restart.sh 默认里，server 无值）；空串口应放行且不落盘。"""

    session_id = login(client)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda: [
        {"name": "USB Audio", "max_input_channels": 2, "max_output_channels": 2},
    ]))
    res = client.put(
        "/api/v1/devices",
        json={"rig_model": 1049, "rig_device": "", "rig_baud": 38400,
              "rigctld_port": 4532, "audio_device": "USB Audio"},
        headers=auth_headers(session_id),
    )
    assert res.status_code == 200, res.text
    saved = json.loads((tmp_path / "device-config.json").read_text())
    assert "rig_device" not in saved
    assert saved["audio_device"] == "USB Audio"


def test_devices_put_rejects_invalid(client, monkeypatch) -> None:
    session_id = login(client)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(query_devices=lambda: []))
    bad = client.put(
        "/api/v1/devices", json={"rig_baud": 12345, "audio_device": "Nope"},
        headers=auth_headers(session_id),
    )
    assert bad.status_code == 422


def test_devices_put_locked_during_tx(client, tmp_path, monkeypatch) -> None:
    session_id = login(client)
    acquire_lease(client)
    client.post("/api/v1/operation/cq", headers=auth_headers(session_id))
    res = client.put(
        "/api/v1/devices", json={"rigctld_port": 4533}, headers=auth_headers(session_id)
    )
    assert res.status_code == 409
    assert res.json()["reason"] == "tx_active"


def test_devices_put_locked_when_ptt_on_only(client, tmp_path) -> None:
    """TX 锁同时检查 safety.ptt_on（非仅 armed）；直接置位验证该分支。"""

    session_id = login(client)
    client.app.state.app_state.safety.ptt_on = True
    res = client.put(
        "/api/v1/devices", json={"rigctld_port": 4533}, headers=auth_headers(session_id)
    )
    assert res.status_code == 409
    assert res.json()["reason"] == "tx_active"


def test_devices_apply_requires_saved_config(client) -> None:
    session_id = login(client)
    res = client.post("/api/v1/devices/apply", headers=auth_headers(session_id))
    assert res.status_code == 409
    assert res.json()["reason"] == "no_device_config"


def test_devices_apply_locked_during_tx(client, tmp_path) -> None:
    save_device_config({"rigctld_port": 4532}, tmp_path / "device-config.json")
    session_id = login(client)
    acquire_lease(client)
    client.post("/api/v1/operation/cq", headers=auth_headers(session_id))
    res = client.post("/api/v1/devices/apply", headers=auth_headers(session_id))
    assert res.status_code == 409
    assert res.json()["reason"] == "tx_active"


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/devices"),
    ("put", "/api/v1/devices"),
    ("post", "/api/v1/devices/apply"),
])
def test_devices_require_session(client, method, path) -> None:
    kwargs = {"json": {}} if method != "get" else {}
    res = getattr(client, method)(path, **kwargs)
    assert res.status_code == 401


def test_devices_apply_spawns_restart(client, tmp_path) -> None:
    save_device_config({"rigctld_port": 4532}, tmp_path / "device-config.json")
    session_id = login(client)
    spy = {"called": 0}
    # TestClient exposes the composed AppState via client.app.state.app_state.
    store = client.app.state.app_state.device_config
    store.spawn_restart = lambda: spy.__setitem__("called", spy["called"] + 1)
    res = client.post("/api/v1/devices/apply", headers=auth_headers(session_id))
    assert res.status_code == 202, res.text
    assert res.json()["restarting"] is True
    assert res.json()["eta_s"] == 20
    assert spy["called"] == 1
