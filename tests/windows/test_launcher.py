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


def test_server_command_includes_ssl_pair() -> None:
    mod = _load()
    cmd = mod.server_command(Path("C:/app/App"), (Path("C:/c.pem"), Path("C:/k.pem")))
    assert cmd[0].endswith("ft8-server.exe")
    assert "--ssl-cert" in cmd and "C:/c.pem" in cmd
    assert "--ssl-key" in cmd and "C:/k.pem" in cmd


def test_wait_for_server_timeout(monkeypatch) -> None:
    mod = _load()
    def urlopen(url, timeout=None, context=None):
        raise ConnectionRefusedError()
    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    now = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    assert mod.wait_for_server("http://127.0.0.1:8000", None, timeout_s=0.5) is False
