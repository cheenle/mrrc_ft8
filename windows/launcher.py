"""MRRC_FT8 Windows launcher (PyInstaller exe entry).

Seeds %LOCALAPPDATA%\\MRRC-FT8\\ft8.env + self-signed certs on first run,
spawns the bundled rigctld.exe (serial owner, AD-008) and ft8-server.exe
bound to 0.0.0.0 with HTTPS, then opens the browser.  Pure functions are
unit-tested; process orchestration is smoke-tested on the build VM.
"""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from windows.ssl_bootstrap import ensure_self_signed

APP_NAME = "MRRC_FT8"
DEFAULT_WEB_PORT = "8000"
DEFAULT_RIGCTLD_PORT = "4532"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def user_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "MRRC-FT8"
    return Path.home() / ".mrrc-ft8"


def config_path() -> Path:
    return user_data_dir() / "ft8.env"


def default_config_path() -> Path:
    return app_dir() / "windows" / "default.env"


def ensure_config() -> Path:
    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    path = config_path()
    if not path.exists():
        default = default_config_path()
        if default.exists():
            path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text(
                "MRRC_FT8_PASSWORD_HASH=\nMRRC_FT8_MY_CALL=N0CALL\n"
                "MRRC_FT8_MY_GRID=AA00AA\nMRRC_FT8_WEB_HOST=0.0.0.0\n"
                "MRRC_FT8_WEB_PORT=8000\n",
                encoding="utf-8",
            )
    return path


def _lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 80))  # RFC 5737 TEST-NET-1, no traffic
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips)


def load_env(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    allowed = ["localhost", socket.gethostname()]
    allowed.extend(_lan_ips())
    env.setdefault("MRRC_FT8_ALLOWED_HOSTS", ",".join(allowed))
    return env


def local_url(env: dict[str, str], secure: bool = False) -> str:
    port = env.get("MRRC_FT8_WEB_PORT", DEFAULT_WEB_PORT)
    host = env.get("MRRC_FT8_WEB_HOST", "127.0.0.1")
    scheme = "https" if secure else "http"
    if host in ("0.0.0.0", "::", ""):
        url_host = "localhost"
    else:
        url_host = host
    return f"{scheme}://{url_host}:{port}"


def rigctld_command(
    env: dict[str, str], device_cfg: dict | None, app: Path | None = None
) -> list[str]:
    if app is None:
        app = app_dir()
    cfg = device_cfg or {}
    def pick(key: str, env_key: str, default: str) -> str:
        return str(cfg.get(key) if cfg.get(key) is not None
                   else env.get(env_key, default))
    model = pick("rig_model", "MRRC_FT8_RIG_MODEL", "1049")
    device = pick("rig_device", "MRRC_FT8_RIG_DEVICE", "COM3")
    baud = pick("rig_baud", "MRRC_FT8_RIG_BAUD", "38400")
    stop = pick("rig_stop_bits", "MRRC_FT8_RIG_STOP_BITS", "1")
    port = pick("rigctld_port", "MRRC_FT8_RIGCTLD_PORT", DEFAULT_RIGCTLD_PORT)
    cmd = [
        str(app / "hamlib" / "rigctld.exe"),
        "-m", model, "-r", device, "-s", baud,
        "-T", "127.0.0.1", "-t", port, "-vvv",
    ]
    if stop != "1":
        cmd += ["-C", f"stop_bits={stop}"]
    return cmd


def server_command(app: Path, ssl_pair: tuple[Path, Path] | None) -> list[str]:
    # ft8-server.exe (Task 1) enables HTTPS only when both --ssl-cert and
    # --ssl-key are given; with no pair it runs plain HTTP (uvicorn_kwargs
    # omits the ssl_* keys). No --no-ssl flag exists — do not add one.
    cmd = [str(app / "ft8-server.exe")]
    if ssl_pair is not None:
        cmd += ["--ssl-cert", str(ssl_pair[0]), "--ssl-key", str(ssl_pair[1])]
    return cmd


def wait_for_server(
    url: str, proc: subprocess.Popen | None = None,
    timeout_s: float = 15.0, secure: bool = False,
) -> bool:
    ctx = None
    if secure:
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    deadline = time.monotonic() + timeout_s
    probe = url + "/api/health"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(probe, timeout=2, context=ctx):
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _load_device_config() -> dict | None:
    path = user_data_dir() / "data" / "device-config.json"
    if not path.exists():
        return None
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _wait_port(host: str, port: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _start_rigctld(env: dict[str, str], device_cfg: dict | None) -> subprocess.Popen | None:
    cmd = rigctld_command(env, device_cfg)
    if not Path(cmd[0]).exists():
        print(f"WARNING: rigctld not found at {cmd[0]}; radio control disabled")
        return None
    log_path = user_data_dir() / "rigctld.log"
    with log_path.open("ab") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
    port = int(env.get("MRRC_FT8_RIGCTLD_PORT", DEFAULT_RIGCTLD_PORT))
    if _wait_port("127.0.0.1", port):
        print(f"rigctld ready on 127.0.0.1:{port}")
    else:
        print("WARNING: rigctld did not open its port in time")
    return proc


def _add_firewall_rule(port: int) -> None:
    """Best-effort inbound allow for the web port (needs admin; ignore failure).

    Inno Setup already adds the rule at install time (MRRC-FT8.iss [Run]
    netsh line); this is a safety net when the port was changed in ft8.env.
    """
    name = "MRRC_FT8 Web"
    args = [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={name}", "dir=in", "action=allow", "protocol=TCP",
        f"localport={port}",
    ]
    try:
        subprocess.run(args, timeout=10, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def main() -> int:
    cfg = ensure_config()
    env = load_env(cfg)
    device_cfg = _load_device_config()

    cert_dir = user_data_dir() / "certs"
    ssl_pair = ensure_self_signed(cert_dir)
    secure = ssl_pair is not None
    url = local_url(env, secure=secure)

    print(APP_NAME)
    print(f"Config: {cfg}")
    print(f"URL:    {url}")
    if secure:
        print("HTTPS: self-signed certificate (browser warns once — accept it)")
    print(f"Password: see {cfg} (MRRC_FT8_PASSWORD_HASH); change with "
          f"`python -m server.main --hash-password NEWPASS`")
    print("Close this window or press Ctrl-C to stop the server.")

    port = int(env.get("MRRC_FT8_WEB_PORT", DEFAULT_WEB_PORT))
    _add_firewall_rule(port)

    rig_proc = _start_rigctld(env, device_cfg)

    command = server_command(app_dir(), ssl_pair)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        command, cwd=str(user_data_dir()), env=env, creationflags=creationflags,
    )

    if wait_for_server(url, proc, secure=secure):
        webbrowser.open(url + "/desktop/")
    elif proc.poll() is not None:
        print("Server exited during startup — see messages above.")
    else:
        print(f"Server did not answer within 15s; opening {url} anyway.")
        webbrowser.open(url + "/desktop/")

    try:
        code = proc.wait()
    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=5)
        code = 0
    if rig_proc is not None and rig_proc.poll() is None:
        rig_proc.kill()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
