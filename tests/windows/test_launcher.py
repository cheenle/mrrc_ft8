from pathlib import Path
from types import SimpleNamespace
import pytest

def _load():
    import importlib.util
    import sys as _sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(
        "ft8_launcher", root / "windows" / "launcher.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_load_env_parses_key_value(tmp_path) -> None:
    mod = _load()
    f = tmp_path / "ft8.env"
    f.write_text("MRRC_FT8_MY_CALL=N0CALL\nMRRC_FT8_WEB_PORT=8443\n# comment\n")
    env = mod.load_env(f)
    assert env["MRRC_FT8_MY_CALL"] == "N0CALL"
    assert env["MRRC_FT8_WEB_PORT"] == "8443"
    assert "comment" not in env


def test_load_env_allowed_hosts_default_contains_localhost(tmp_path) -> None:
    mod = _load()
    f = tmp_path / "ft8.env"
    f.write_text("MRRC_FT8_MY_CALL=N0CALL\n")
    env = mod.load_env(f)
    assert "MRRC_FT8_ALLOWED_HOSTS" in env
    assert "localhost" in env["MRRC_FT8_ALLOWED_HOSTS"]


def test_load_env_allowed_hosts_explicit_wins(tmp_path) -> None:
    mod = _load()
    f = tmp_path / "ft8.env"
    f.write_text("MRRC_FT8_ALLOWED_HOSTS=192.168.1.50\n")
    env = mod.load_env(f)
    assert env["MRRC_FT8_ALLOWED_HOSTS"] == "192.168.1.50"


def test_local_url_https_localhost() -> None:
    mod = _load()
    env = {"MRRC_FT8_WEB_HOST": "0.0.0.0", "MRRC_FT8_WEB_PORT": "8000"}
    assert mod.local_url(env, secure=True) == "https://localhost:8000"


def test_rigctld_command_builds_hamlib_args(monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "app_dir", lambda: Path("C:/app/App"))
    env = {"MRRC_FT8_RIG_MODEL": "1049", "MRRC_FT8_RIG_DEVICE": "COM3",
           "MRRC_FT8_RIG_BAUD": "38400", "MRRC_FT8_RIGCTLD_PORT": "4532"}
    cmd = mod.rigctld_command(env, None, app=Path("C:/app/App"))
    assert cmd[0].replace("/", "\\").endswith("hamlib\\rigctld.exe")
    assert "-m" in cmd and "1049" in cmd and "-r" in cmd and "COM3" in cmd


def test_rigctld_command_adds_stop_bits_when_not_one() -> None:
    mod = _load()
    env = {"MRRC_FT8_RIG_DEVICE": "COM3", "MRRC_FT8_RIG_STOP_BITS": "2"}
    cmd = mod.rigctld_command(env, None, app=Path("C:/app/App"))
    assert "-C" in cmd and "stop_bits=2" in cmd


def test_rigctld_command_baked_defaults() -> None:
    mod = _load()
    cmd = mod.rigctld_command({}, None, app=Path("C:/app/App"))
    assert cmd[cmd.index("-m") + 1] == "1049"
    assert cmd[cmd.index("-r") + 1] == "COM3"
    assert cmd[cmd.index("-s") + 1] == "38400"
    assert cmd[cmd.index("-t") + 1] == "4532"
    assert "-vvv" in cmd


def test_rigctld_command_no_stop_bits_flag_when_default() -> None:
    mod = _load()
    cmd = mod.rigctld_command({}, None, app=Path("C:/app/App"))
    assert "-C" not in cmd
    cmd = mod.rigctld_command({"MRRC_FT8_RIG_STOP_BITS": "1"}, None, app=Path("C:/app/App"))
    assert "-C" not in cmd


def test_rigctld_command_device_cfg_overrides_env() -> None:
    mod = _load()
    cmd = mod.rigctld_command(
        {"MRRC_FT8_RIG_MODEL": "1049"},
        {"rig_model": 3073},
        app=Path("C:/app/App"),
    )
    assert cmd[cmd.index("-m") + 1] == "3073"


def test_server_command_includes_ssl_pair() -> None:
    mod = _load()
    cmd = mod.server_command(Path("C:/app/App"), (Path("C:/c.pem"), Path("C:/k.pem")))
    assert cmd[0].endswith("ft8-server.exe")
    assert "--ssl-cert" in cmd and "C:/c.pem" in cmd
    assert "--ssl-key" in cmd and "C:/k.pem" in cmd


def test_server_command_without_ssl_pair() -> None:
    mod = _load()
    cmd = mod.server_command(Path("C:/app/App"), None)
    assert cmd == ["C:/app/App/ft8-server.exe"]
    assert "--ssl-cert" not in cmd
    assert "--ssl-key" not in cmd
    assert "--no-ssl" not in cmd


def test_wait_for_server_timeout(monkeypatch) -> None:
    mod = _load()
    def urlopen(url, timeout=None, context=None):
        raise ConnectionRefusedError()
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    now = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    assert mod.wait_for_server("http://127.0.0.1:8000", None, timeout_s=0.5) is False
