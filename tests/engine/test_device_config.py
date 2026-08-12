from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from server.engine.device_config import (
    BAUD_RATES,
    CURATED_RIG_MODELS,
    DeviceConfigStore,
    enumerate_audio_devices,
    enumerate_serial_devices,
    load_device_config,
    merge_into,
    save_device_config,
    shell_env_lines,
    source_of,
    validate,
)


def test_curated_models_and_bauds_match_station_hamlib() -> None:
    assert {m for m, _ in CURATED_RIG_MODELS} == {1020, 1049, 3073, 30003}
    assert 38400 in BAUD_RATES and 115200 in BAUD_RATES


def test_load_missing_file_returns_none(tmp_path) -> None:
    assert load_device_config(tmp_path / "absent.json") is None


def test_load_roundtrip_filters_unknown_keys(tmp_path) -> None:
    path = tmp_path / "device-config.json"
    path.write_text(json.dumps({"rig_model": 1049, "audio_device": "USB", "nonsense": 1}))
    assert load_device_config(path) == {"rig_model": 1049, "audio_device": "USB"}


def test_load_corrupt_json_returns_none(tmp_path) -> None:
    path = tmp_path / "device-config.json"
    path.write_text("{not json")
    assert load_device_config(path) is None


def test_save_is_atomic_and_creates_parent(tmp_path) -> None:
    path = tmp_path / "nested" / "device-config.json"
    save_device_config({"rig_model": 1049, "rigctld_port": 4532, "audio_device": None}, path)
    assert json.loads(path.read_text()) == {"rig_model": 1049, "rigctld_port": 4532}
    leftovers = list(tmp_path.glob("nested/device-config-*.tmp"))
    assert leftovers == []


def test_shell_env_lines_skip_none(tmp_path) -> None:
    lines = shell_env_lines({"rig_model": 1049, "rig_device": "/dev/cu.x", "audio_device": None})
    assert lines == ["MRRC_FT8_RIG_MODEL=1049", "MRRC_FT8_RIG_DEVICE='/dev/cu.x'"]


def test_merge_into_overrides_audio_and_rigctld_port() -> None:
    from server.main import ServerConfig

    base = ServerConfig(
        password_hash="h", my_call="M0XX", my_grid="IO91",
        allowed_hosts=frozenset({"localhost"}),
    )
    merged = merge_into(base, {"audio_device": "USB Audio", "rigctld_port": 4533})
    assert merged.audio_device == "USB Audio"
    assert merged.rigctld_port == 4533
    assert merged.my_call == base.my_call  # unrelated fields untouched
    assert merge_into(base, None) is base


def test_enumerate_audio_devices_maps_rows(monkeypatch) -> None:
    fake = SimpleNamespace(query_devices=lambda: [
        {"name": "A", "max_input_channels": 1, "max_output_channels": 2},
        {"name": "B", "max_input_channels": 0, "max_output_channels": 8},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    assert enumerate_audio_devices() == [
        {"index": 0, "name": "A", "max_input": 1, "max_output": 2},
        {"index": 1, "name": "B", "max_input": 0, "max_output": 8},
    ]


def test_enumerate_audio_devices_tolerates_missing_sounddevice(monkeypatch) -> None:
    # sounddevice is a hard repo dependency (pyproject.toml), so removing it
    # from sys.modules would just re-import it from disk; a None entry makes
    # ``import sounddevice`` raise ImportError — the real "absent dependency"
    # path the module must tolerate.
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert enumerate_audio_devices() == []


def test_enumerate_serial_devices(monkeypatch) -> None:
    monkeypatch.setattr(
        "server.engine.device_config.glob.glob",
        lambda pat: [f"/dev/cu.usbserial-0121DB3A0"] if "cu." in pat else [],
    )
    assert enumerate_serial_devices() == ["/dev/cu.usbserial-0121DB3A0"]


def test_enumerate_serial_devices_windows_com_ports(monkeypatch) -> None:
    # Import pyserial while os.name is still "posix" so its module gets cached
    # in sys.modules; faking os.name="nt" first would force the Windows-only
    # serial.win32 (ctypes.WinDLL) to load, which fails on non-Windows hosts.
    import serial.tools.list_ports as lp
    monkeypatch.setattr(
        lp,
        "comports",
        lambda: [
            type("P", (), {"device": "COM3"})(),
            type("P", (), {"device": "COM5"})(),
        ],
    )
    monkeypatch.setattr("server.engine.device_config.os.name", "nt")
    from server.engine.device_config import enumerate_serial_devices
    assert enumerate_serial_devices() == ["COM3", "COM5"]


def test_validate_accepts_windows_com_port(monkeypatch) -> None:
    monkeypatch.setattr("server.engine.device_config.os.name", "nt")
    from server.engine.device_config import validate
    assert validate({"rig_device": "COM3"}, []) is None
    assert validate({"rig_device": "com7"}, []) is None


def test_validate_rejects_non_com_on_windows(monkeypatch) -> None:
    monkeypatch.setattr("server.engine.device_config.os.name", "nt")
    from server.engine.device_config import validate
    assert validate({"rig_device": "/dev/cu.usbserial-1"}, []) is not None


def test_validate_accepts_good_config() -> None:
    cfg = {"rig_model": 1049, "rig_device": "/dev/cu.x", "rig_baud": 38400,
           "rigctld_port": 4532, "audio_device": "USB"}
    assert validate(cfg, [{"index": 0, "name": "USB"}]) is None


@pytest.mark.parametrize("bad", [
    {"rig_model": 0},
    {"rig_model": "x"},
    {"rig_device": "usb"},
    {"rig_device": 5},
    {"rig_baud": 12345},
    {"rig_stop_bits": 0},
    {"rig_stop_bits": 3},
    {"rig_stop_bits": "2"},
    {"rig_stop_bits": True},
    {"rigctld_port": 80},
    {"rigctld_port": 8000},
    {"audio_device": "Not-There"},
    {"rig_model": True},
    {"audio_device": True},
])
def test_validate_rejects(bad: dict) -> None:
    assert validate(bad, [{"index": 0, "name": "USB"}]) is not None


def test_validate_allows_empty_rig_device_as_unset() -> None:
    """空串口 = 不改动（UI 表单总是提交该字段，本站串口只在 restart.sh 默认里）。"""

    assert validate({"rig_device": ""}, []) is None
    assert validate({"rig_device": "  "}, []) is None


def test_validate_accepts_stop_bits_1_and_2() -> None:
    assert validate({"rig_stop_bits": 1}, []) is None
    assert validate({"rig_stop_bits": 2}, []) is None


def test_save_skips_empty_rig_device(tmp_path) -> None:
    path = tmp_path / "device-config.json"
    save_device_config({"rig_device": "", "audio_device": "FT8"}, path)
    assert json.loads(path.read_text()) == {"audio_device": "FT8"}


def test_validate_allows_unchanged_audio_even_when_absent() -> None:
    assert validate({"audio_device": "USB"}, [], current_effective="USB") is None


def test_source_of_priority(monkeypatch) -> None:
    env = {"MRRC_FT8_AUDIO_DEVICE": "USB", "MRRC_FT8_RIG_MODEL": "1049", "MRRC_FT8_RIG_STOP_BITS": "2"}
    src = source_of({"audio_device": "Other"}, environ=env)
    assert src["audio_device"] == "file"
    assert src["rig_model"] == "env"
    assert src["rig_stop_bits"] == "env"
    assert src["rigctld_port"] == "default"

    # Server-side env for rigctld_port is MRRC_FT8_RIGCTLD (host:port); the
    # restart.sh launch var MRRC_FT8_RIGCTLD_PORT is not a server-side source.
    assert source_of({}, environ={"MRRC_FT8_RIGCTLD": "127.0.0.1:4533"})["rigctld_port"] == "env"
    assert source_of({}, environ={"MRRC_FT8_RIGCTLD_PORT": "4533"})["rigctld_port"] == "default"


def test_store_roundtrip_and_spawn_script(tmp_path) -> None:
    store = DeviceConfigStore(tmp_path / "d.json", script="/nonexistent/restart.sh")
    store.save({"rig_model": 3073})
    assert store.load() == {"rig_model": 3073}


def test_enumerate_audio_devices_times_out_on_wedged_sounddevice(monkeypatch) -> None:
    """CoreAudio 卡死（聚合设备引用断电电台）时枚举必须超时返回 []，不能挂死。"""

    import builtins
    import time as _time

    real_import = builtins.__import__

    def hang_on_sounddevice(name, *args, **kwargs):
        if name == "sounddevice":
            _time.sleep(60)  # 模拟 PortAudio 枚举挂起
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", hang_on_sounddevice)
    t0 = _time.monotonic()
    assert enumerate_audio_devices(timeout=1.0) == []
    assert _time.monotonic() - t0 < 5


def test_validate_accepts_input_channel_0_and_1() -> None:
    assert validate({"audio_in_channel": 0}, []) is None
    assert validate({"audio_in_channel": 1}, []) is None


@pytest.mark.parametrize("bad", [
    {"audio_in_channel": 2},
    {"audio_in_channel": -1},
    {"audio_in_channel": "1"},
    {"audio_in_channel": True},
])
def test_validate_rejects_input_channel(bad: dict) -> None:
    assert validate(bad, []) is not None
