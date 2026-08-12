# Windows 11/10 安装包（MRRC_FT8）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows 11/10 installer `MRRC_FT8-Setup.exe` for the full MRRC-FT8 station, mirroring the mrrc_ft710 packaging pattern (PyInstaller + launcher + Inno Setup) on the shared Win11 KVM VM.

**Architecture:** The Python FastAPI server + React desktop client + Fortran DSP DLL are bundled by PyInstaller into `ft8-server.exe` and a launcher `MRRC_FT8.exe`; Inno Setup assembles `MRRC_FT8-Setup.exe`. The launcher seeds config/certs into `%LOCALAPPDATA%\MRRC-FT8`, spawns the bundled `rigctld.exe` (serial owner, AD-008) and the server bound to `0.0.0.0` with self-signed HTTPS, then opens the browser. Repo-side server changes (host/port/SSL config, frozen paths, Windows serial/restart) are TDD'd on the Mac; packaging artifacts are verified by smoke test on the VM.

**Tech Stack:** Python 3.11+ / FastAPI / uvicorn, PyInstaller 6.21, Inno Setup 6, MinGW-w64 gfortran (one-time DSP build), Node.js (desktop client build), Hamlib rigctld, msys2.

## 执行状态（2026-08-12）

- **Task 1–10：已完成并提交**（`feat/windows-installer`，提交见 git log：f13d465 … bca9b7e）。
- **Task 11（文档）：已完成并提交** `8ba8e2e`（`win_pack.md` + `docs/WINDOWS_INSTALLER_GUIDE.md`）。
- **Windows 跨平台修复：已提交** `1b54b50`（build.ps1 语法、PyInstaller spec parents 索引、`server_entry.py` freeze_support 包装器、binding 大栈线程 + DLL 搜索路径、win32 shm 页对齐、测试平台化/GBK/skipif）。
- **Task 12（VM 构建 + 冒烟）：已完成**。在 Win11 KVM VM 上：
  - pytest **832 passed, 78 skipped**（skips 是文档化的 Windows 不适用项：raw-ctypes 解码栈、导出 map、打开文件 os.replace、同进程锁竞争、desktop dist 未建）。
  - `dist\windows\MRRC_FT8-Setup.exe` 构建成功（60 MB，SHA-256 `ee866947731ac2318f4c1c7dc14511ba8a67b67ffbb68a2f32c8d9a3f68318b2`）。
  - 冻结版 `ft8-server.exe` 冒烟通过：启动、绑定 8000、`/api/v1/health` 401、DSP/capture 子进程正常 spawn（freeze_support）。
  - 剩余手工项：VM 桌面安装 Setup.exe 后完整 UI 冒烟（登录、LAN 访问、Devices Apply）——照 `win_pack.md` §3 Step 7。
- Mac 侧测试全绿：`venv/bin/python -m pytest tests/ -q` → 908 passed。

## Global Constraints

- Server audio is always 12 kHz int16 mono into the decoder; TX is 48 kHz. Do not touch.
- All DSP calls go through the `server/core/binding.py` global lock. Do not touch binding ABI.
- rigctld is the sole serial owner; no module may open serial directly.
- `wsjtx-3.0.2/` is read-only vendor source — never modify.
- App name `MRRC_FT8`, version `1.1.0`, first-run password `abcd1234` (Argon2id hash baked into `windows/default.env`).
- LAN/remote access: server binds `0.0.0.0`, self-signed HTTPS, `MRRC_FT8_ALLOWED_HOSTS` must include the machine hostname + LAN IPs (launcher computes it).
- Build happens on the Win11 KVM VM (`ham.vlsc.net` → `192.168.122.133`), same one mrrc_ft710 uses.
- Repo-side code changes must keep the existing macOS/Linux behavior unchanged (env defaults preserved).

---

### Task 1: Server web host/port/SSL configuration

**Files:**
- Modify: `server/main.py:78-201` (ServerConfig fields + from_env), `server/main.py:1088-1125` (main CLI + uvicorn.run)
- Test: `tests/web/test_main.py` (add new tests)

**Interfaces:**
- Consumes: existing `ServerConfig` dataclass and `create_server(config)`.
- Produces: `ServerConfig.web_host: str`, `ServerConfig.web_port: int`; `uvicorn_kwargs(config, ssl_cert, ssl_key) -> dict` helper.

- [ ] **Step 1: Write the failing tests**

Add to `tests/web/test_main.py`:

```python
def test_from_env_reads_web_host_port(monkeypatch) -> None:
    monkeypatch.setenv("MRRC_FT8_PASSWORD_HASH", "x")
    monkeypatch.setenv("MRRC_FT8_MY_CALL", "N0CALL")
    monkeypatch.setenv("MRRC_FT8_MY_GRID", "AA00AA")
    monkeypatch.setenv("MRRC_FT8_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("MRRC_FT8_WEB_PORT", "8443")
    from server.main import ServerConfig
    cfg = ServerConfig.from_env()
    assert cfg.web_host == "0.0.0.0"
    assert cfg.web_port == 8443


def test_from_env_defaults_web_host_port(monkeypatch) -> None:
    monkeypatch.delenv("MRRC_FT8_WEB_HOST", raising=False)
    monkeypatch.delenv("MRRC_FT8_WEB_PORT", raising=False)
    monkeypatch.setenv("MRRC_FT8_PASSWORD_HASH", "x")
    monkeypatch.setenv("MRRC_FT8_MY_CALL", "N0CALL")
    monkeypatch.setenv("MRRC_FT8_MY_GRID", "AA00AA")
    from server.main import ServerConfig
    cfg = ServerConfig.from_env()
    assert cfg.web_host == "127.0.0.1"
    assert cfg.web_port == 8000


def test_uvicorn_kwargs_applies_ssl() -> None:
    from server.main import ServerConfig, uvicorn_kwargs
    cfg = ServerConfig("h", "c", "g", frozenset({"localhost"}))
    kw = uvicorn_kwargs(cfg, ssl_cert="c.pem", ssl_key="k.pem")
    assert kw["host"] == "127.0.0.1" and kw["port"] == 8000
    assert kw["ssl_certfile"] == "c.pem" and kw["ssl_keyfile"] == "k.pem"


def test_uvicorn_kwargs_omits_ssl_when_absent() -> None:
    from server.main import ServerConfig, uvicorn_kwargs
    cfg = ServerConfig("h", "c", "g", frozenset({"localhost"}))
    kw = uvicorn_kwargs(cfg)
    assert "ssl_certfile" not in kw and "ssl_keyfile" not in kw
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/web/test_main.py::test_from_env_reads_web_host_port -v`
Expected: FAIL — `AttributeError: 'ServerConfig' object has no attribute 'web_host'`.

- [ ] **Step 3: Implement**

In `server/main.py`, add fields to `ServerConfig`:

```python
    web_host: str = "127.0.0.1"
    web_port: int = 8000
```

In `from_env`, before the `return cls(...)`:

```python
        web_host = os.environ.get("MRRC_FT8_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
        try:
            web_port = int(os.environ.get("MRRC_FT8_WEB_PORT", "8000"))
        except ValueError:
            raise ValueError("MRRC_FT8_WEB_PORT must be an integer")
```

Add to the `return cls(...)` call:

```python
            web_host=web_host,
            web_port=web_port,
```

Add the helper after `main()`'s module level (place next to `create_server`):

```python
def uvicorn_kwargs(
    config: ServerConfig,
    *,
    ssl_cert: str | None = None,
    ssl_key: str | None = None,
) -> dict[str, object]:
    """Uvicorn run kwargs from config + optional TLS pair."""
    kw: dict[str, object] = {"host": config.web_host, "port": config.web_port}
    if ssl_cert and ssl_key:
        kw["ssl_certfile"] = ssl_cert
        kw["ssl_keyfile"] = ssl_key
    return kw
```

In `main()`, add the two CLI args and use the helper:

```python
    parser.add_argument("--ssl-cert", default=None, metavar="PEM",
                        help="TLS certificate file (enables HTTPS)")
    parser.add_argument("--ssl-key", default=None, metavar="PEM",
                        help="TLS private key file (enables HTTPS)")
    ...
    uvicorn.run(app, **uvicorn_kwargs(config, ssl_cert=args.ssl_cert, ssl_key=args.ssl_key))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/web/test_main.py -q`
Expected: PASS (all pre-existing + new tests).

- [ ] **Step 5: Run the full suite**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add server/main.py tests/web/test_main.py
git commit -m "feat(server): configurable web host/port + optional HTTPS (Windows installer prep)"
```

---

### Task 2: Frozen-mode app-root path helper

**Files:**
- Create: `server/core/paths.py`
- Modify: `server/main.py:1057-1066` (`_static_dir`, `_desktop_dist_dir`)
- Modify: `server/engine/dxcc.py:239`
- Test: `tests/core/test_paths.py`

**Interfaces:**
- Produces: `app_root() -> Path` — repo root in source, `_MEIPASS` (PyInstaller `_internal/`) when frozen.
- Consumes: nothing.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_paths.py`:

```python
import sys
import pytest

def test_app_root_source_tree() -> None:
    from server.core.paths import app_root
    root = app_root()
    assert (root / "server" / "main.py").exists()
    assert (root / "cty.dat").exists()


def test_app_root_frozen_uses_meipass(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/virtual/_internal", raising=False)
    from server.core.paths import app_root
    assert str(app_root()) == "/virtual/_internal"


def test_app_root_frozen_falls_back_to_exe_dir(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", "/app/App/MRRC_FT8.exe", raising=False)
    from server.core.paths import app_root
    assert str(app_root()) == "/app/App"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/core/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.core.paths'`.

- [ ] **Step 3: Implement `server/core/paths.py`**

```python
"""Application-root resolution that works in-source and PyInstaller-frozen.

PyInstaller 6 onedir unpacks datas and the bytecode into ``_internal/`` and
points ``sys._MEIPASS`` there; ``Path(__file__)`` for PYZ-bundled modules does
not reliably exist on disk.  Every read-only bundled asset (cty.dat, web
static, desktop dist, wsjt_core.dll) is resolved through :func:`app_root` so
the same code works from a checkout and from the frozen installer.
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    """Repo root in source; PyInstaller ``_internal/`` when frozen."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]
```

- [ ] **Step 4: Wire it into the three call sites**

`server/main.py` `_static_dir`:

```python
def _static_dir() -> str:
    from server.core.paths import app_root

    return str(app_root() / "server" / "web" / "static")
```

`server/main.py` `_desktop_dist_dir`:

```python
def _desktop_dist_dir() -> str:
    from server.core.paths import app_root

    return str(app_root() / "desktop" / "ft8web" / "dist")
```

`server/engine/dxcc.py` around line 239:

```python
        from server.core.paths import app_root
        _cty_db = load_cty(str(app_root() / "cty.dat"))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/core/test_paths.py tests/web/test_static.py tests/engine/test_dxcc.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add server/core/paths.py server/main.py server/engine/dxcc.py tests/core/test_paths.py
git commit -m "feat(server): PyInstaller-frozen app-root resolution for bundled assets"
```

---

### Task 3: Windows serial device enumeration + validation

**Files:**
- Modify: `server/engine/device_config.py`
- Test: `tests/engine/test_device_config.py`

**Interfaces:**
- Produces: `enumerate_serial_devices() -> list[str]` now returns Windows COM ports on `os.name == "nt"`.
- `validate(cfg, audio_devices)` accepts `COM\d+` rig_device on Windows.

- [ ] **Step 1: Write the failing tests**

Add to `tests/engine/test_device_config.py`:

```python
def test_enumerate_serial_devices_windows_com_ports(monkeypatch) -> None:
    monkeypatch.setattr("server.engine.device_config.os.name", "nt")
    import serial.tools.list_ports as lp
    monkeypatch.setattr(
        lp,
        "comports",
        lambda: [
            type("P", (), {"device": "COM3"})(),
            type("P", (), {"device": "COM5"})(),
        ],
    )
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
```

Note: `pyserial` (`serial`) is required for the Windows enumeration path. Add it to the test env: `venv/bin/pip install pyserial` (and it will be added to `pyproject.toml` in Step 3).

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/engine/test_device_config.py -q`
Expected: FAIL — enumeration returns `[]` on non-Windows branch; validation rejects `COM3`.

- [ ] **Step 3: Implement**

In `server/engine/device_config.py`, replace `enumerate_serial_devices`:

```python
def enumerate_serial_devices() -> list[str]:
    """Candidate CAT serial devices (macOS/Linux /dev names, Windows COM)."""
    if os.name == "nt":
        try:
            from serial.tools import list_ports
            return sorted(p.device for p in list_ports.comports())
        except Exception:
            return sorted(glob.glob("COM[0-9]*"))
    return sorted(
        set(glob.glob("/dev/cu.*")) | set(glob.glob("/dev/ttyUSB*")) | set(glob.glob("/dev/ttyACM*"))
    )
```

In `validate`, replace the `rig_device` check (lines ~250-259) with a platform-aware version:

```python
        if device:
            if os.name == "nt":
                if not re.fullmatch(r"COM\d{1,3}", device.upper()):
                    return "rig_device must be a COM port (e.g. COM3)"
            elif not device.startswith("/dev/"):
                return "rig_device must be an absolute /dev/... path"
```

Add `import re` at the top of the module if not already present.

Add `pyserial>=3.5` to `pyproject.toml` `[project.dependencies]`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/engine/test_device_config.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add server/engine/device_config.py pyproject.toml tests/engine/test_device_config.py
git commit -m "feat(server): Windows COM port enumeration + validation for device config"
```

---

### Task 4: Windows restart script (spawn_restart) + restart.ps1

**Files:**
- Modify: `server/engine/device_config.py:311-341` (DeviceConfigStore)
- Create: `windows/restart.ps1`
- Test: `tests/engine/test_device_config.py`

**Interfaces:**
- Consumes: `DeviceConfigStore.script` (default resolves platform-appropriate restart script).
- Produces: `DeviceConfigStore.spawn_restart()` launches `restart.ps1` detached on Windows.

- [ ] **Step 1: Write the failing test**

Add to `tests/engine/test_device_config.py`:

```python
def test_spawn_restart_windows_uses_powershell(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("server.engine.device_config.os.name", "nt")
    script = tmp_path / "restart.ps1"
    script.write_text("")
    captured: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        captured.append(cmd)
        class P:
            pass
        return P()

    monkeypatch.setattr("server.engine.device_config.subprocess.Popen", fake_popen)
    monkeypatch.setattr("server.engine.device_config.os.environ", {"LOCALAPPDATA": str(tmp_path)})
    from server.engine.device_config import DeviceConfigStore
    store = DeviceConfigStore(tmp_path / "device-config.json", script=script)
    store.spawn_restart()
    assert captured and captured[0][0].lower() == "powershell"
    assert str(script) in captured[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/engine/test_device_config.py::test_spawn_restart_windows_uses_powershell -v`
Expected: FAIL — the current `spawn_restart` builds `[str(self.script)]` (bash) regardless of platform.

- [ ] **Step 3: Implement the Windows branch in `spawn_restart`**

In `server/engine/device_config.py`, change the default script resolution and add the branch:

```python
class DeviceConfigStore:
    """The API's seam: load/save the config file and spawn the restart script."""

    def __init__(
        self, path: str | Path = DEFAULT_CONFIG_PATH, *, script: str | Path | None = None
    ) -> None:
        self.path = Path(path)
        if script is not None:
            self.script = Path(script)
        elif os.name == "nt":
            from server.core.paths import app_root
            self.script = app_root() / "restart.ps1"
        else:
            self.script = Path(__file__).resolve().parents[2] / "restart.sh"

    def spawn_restart(self) -> None:
        """Detached restart run; survives this process being killed."""

        if not self.script.exists():
            log.error("restart script missing: %s", self.script)
            raise FileNotFoundError(f"restart script missing: {self.script}")
        if os.name == "nt":
            data_dir = Path(os.environ.get("LOCALAPPDATA", ".")) / "MRRC-FT8"
            data_dir.mkdir(parents=True, exist_ok=True)
            log_path = data_dir / "restart.log"
            flags = 0
            for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
                flags |= getattr(subprocess, name, 0)
            with log_path.open("ab") as fh:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(self.script)],
                    cwd=str(self.script.parent),
                    stdout=fh,
                    stderr=fh,
                    creationflags=flags,
                    close_fds=True,
                )
            return
        log_path = Path("/tmp/mrrc-ft8-restart.log")
        with log_path.open("ab") as fh:
            subprocess.Popen(
                [str(self.script)],
                cwd=str(self.script.parent),
                stdout=fh,
                stderr=fh,
                start_new_session=True,
                close_fds=True,
            )
```

- [ ] **Step 4: Create `windows/restart.ps1`**

```powershell
# Restart rigctld + MRRC_FT8 server on Windows (mirrors restart.sh).
# Invoked detached by the server's /api/v1/devices/apply. Reads the same
# env as the launcher (%LOCALAPPDATA%\MRRC-FT8\ft8.env) and relaunches the
# bundled rigctld.exe and ft8-server.exe. Does not reopen the browser.
$ErrorActionPreference = "Stop"

$dataDir = Join-Path $env:LOCALAPPDATA "MRRC-FT8"
$appDir  = Split-Path $PSScriptRoot -Parent   # one dir above _internal

# ---- load env file ----
$envFile = Join-Path $dataDir "ft8.env"
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object {
        $_ -match "^\s*[A-Za-z0-9_]+\s*=" -and $_ -notmatch "^\s*#"
    } | ForEach-Object {
        $kv = $_ -split "=", 2
        [Environment]::SetEnvironmentVariable($kv[0].Trim(), $kv[1].Trim(), "Process")
    }
}

# ---- stop stale processes ----
Get-Process -Name "ft8-server","rigctld" -ErrorAction SilentlyContinue | Stop-Process -Force

# ---- rigctld ----
# Precedence matches restart.sh: device-config.json overrides env, which
# overrides the baked defaults below.
$deviceCfg = Join-Path $dataDir "data\device-config.json"
$rigModel = if ($env:MRRC_FT8_RIG_MODEL)  { $env:MRRC_FT8_RIG_MODEL }  else { "1049" }
$rigDevice = if ($env:MRRC_FT8_RIG_DEVICE) { $env:MRRC_FT8_RIG_DEVICE } else { "COM3" }
$rigBaud = if ($env:MRRC_FT8_RIG_BAUD)    { $env:MRRC_FT8_RIG_BAUD }   else { "38400" }
$rigStop = if ($env:MRRC_FT8_RIG_STOP_BITS) { $env:MRRC_FT8_RIG_STOP_BITS } else { "1" }
$rigPort = if ($env:MRRC_FT8_RIGCTLD_PORT)  { $env:MRRC_FT8_RIGCTLD_PORT }  else { "4532" }
if (Test-Path $deviceCfg) {
    $cfg = Get-Content $deviceCfg -Raw | ConvertFrom-Json
    if ($cfg.rig_model)    { $rigModel  = [string]$cfg.rig_model }
    if ($cfg.rig_device)   { $rigDevice = [string]$cfg.rig_device }
    if ($cfg.rig_baud)     { $rigBaud   = [string]$cfg.rig_baud }
    if ($cfg.rig_stop_bits){ $rigStop   = [string]$cfg.rig_stop_bits }
    if ($cfg.rigctld_port) { $rigPort   = [string]$cfg.rigctld_port }
}

$rigExe = Join-Path $appDir "hamlib\rigctld.exe"
if (Test-Path $rigExe) {
    $rigArgs = @("-m", $rigModel, "-r", $rigDevice, "-s", $rigBaud,
                 "-T", "127.0.0.1", "-t", $rigPort, "-vvv")
    if ($rigStop -ne "1") { $rigArgs += @("-C", "stop_bits=$rigStop") }
    Start-Process -FilePath $rigExe -ArgumentList $rigArgs -WindowStyle Hidden |
        Out-Null
}

# ---- server ----
$serverExe = Join-Path $appDir "ft8-server.exe"
if (Test-Path $serverExe) {
    Start-Process -FilePath $serverExe -WorkingDirectory $dataDir -WindowStyle Hidden |
        Out-Null
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/engine/test_device_config.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add server/engine/device_config.py windows/restart.ps1 tests/engine/test_device_config.py
git commit -m "feat(server): Windows restart.ps1 spawn for device-config apply (AD-008 preserved)"
```

---

### Task 5: DSP library path for Windows + multiprocessing freeze_support

**Files:**
- Modify: `server/core/worker.py:26-30`
- Modify: `server/main.py:1128-1129`
- Test: `tests/core/test_worker.py`

**Interfaces:**
- Produces: `default_library_path() -> Path` returns `wsjt_core.dll` under `app_root()` when frozen, `dsp/build/wsjt_core.dll` on win32 source.

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_worker.py`:

```python
def test_default_library_path_win32_source(monkeypatch) -> None:
    monkeypatch.setattr("server.core.worker.sys.platform", "win32")
    from server.core.worker import default_library_path
    assert default_library_path().name == "wsjt_core.dll"


def test_default_library_path_frozen_uses_app_root(monkeypatch) -> None:
    monkeypatch.setattr("server.core.worker.sys.platform", "win32")
    monkeypatch.setattr("server.core.worker.sys", "frozen", True, raising=False)
    import server.core.paths as paths
    monkeypatch.setattr(paths.sys, "_MEIPASS", "/virtual/_internal", raising=False)
    from server.core.worker import default_library_path
    assert str(default_library_path()) == "/virtual/_internal/wsjt_core.dll"
```

Note: the frozen test relies on `app_root()` being called inside `default_library_path`; when `sys.frozen` is set, `paths.app_root()` reads `sys._MEIPASS`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/core/test_worker.py -q`
Expected: FAIL — `.so` suffix returned for win32.

- [ ] **Step 3: Implement**

Replace `default_library_path` in `server/core/worker.py`:

```python
def default_library_path() -> Path:
    """Return the DSP library path for this platform / mode.

    Frozen installers bundle ``wsjt_core.dll`` and its runtime DLLs at the
    PyInstaller ``_internal`` root; source builds keep the conventional
    ``dsp/build`` CMake output.
    """
    if getattr(sys, "frozen", False):
        from server.core.paths import app_root
        return app_root() / "wsjt_core.dll"
    base = Path(__file__).resolve().parents[2] / "dsp" / "build"
    if sys.platform == "darwin":
        return base / "libwsjt_core.dylib"
    if sys.platform == "win32":
        return base / "wsjt_core.dll"
    return base / "libwsjt_core.so"
```

In `server/main.py`, add `multiprocessing.freeze_support()` to the entry point (required for `mp.get_context("spawn")` workers in a frozen Windows exe — the DSP Worker and capture child both spawn):

```python
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/core/test_worker.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add server/core/worker.py server/main.py tests/core/test_worker.py
git commit -m "feat(server): Windows wsjt_core.dll resolution + multiprocessing freeze_support"
```

---

### Task 6: SSL bootstrap module

**Files:**
- Create: `windows/__init__.py`
- Create: `windows/ssl_bootstrap.py`
- Test: `tests/windows/test_ssl_bootstrap.py`

**Interfaces:**
- Produces: `ensure_self_signed(cert_dir: Path) -> tuple[Path, Path] | None` — self-signed cert/key (10-year, SANs for localhost/hostname/LAN IPs), `None` if cryptography unavailable.

- [ ] **Step 1: Write the failing tests**

Create `tests/windows/test_ssl_bootstrap.py`:

```python
from pathlib import Path
import pytest

def test_ensure_self_signed_generates_pair(tmp_path) -> None:
    from windows.ssl_bootstrap import ensure_self_signed
    pair = ensure_self_signed(tmp_path / "certs")
    assert pair is not None
    cert, key = pair
    assert cert.exists() and key.exists()
    assert cert.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert key.read_bytes().startswith(b"-----BEGIN")


def test_ensure_self_signed_reuses_existing(tmp_path) -> None:
    from windows.ssl_bootstrap import ensure_self_signed
    first = ensure_self_signed(tmp_path)
    second = ensure_self_signed(tmp_path)
    assert first == second
    assert first[0].read_bytes() == second[0].read_bytes()
```

Prerequisite: install `cryptography` into the venv (`venv/bin/pip install cryptography`). It is a launcher-only dependency; record it in `requirements-build.txt` (Task 8).

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/windows/test_ssl_bootstrap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'windows.ssl_bootstrap'`.

- [ ] **Step 3: Implement**

Create `windows/__init__.py` (empty). Copy `ssl_bootstrap.py` verbatim from `mrrc_ft710/ssl_bootstrap.py` (it is self-contained, GPL-compatible, and already battle-tested), changing only the module docstring's app name references to MRRC_FT8. Its public API is `ensure_self_signed(cert_dir) -> tuple[Path, Path] | None`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/windows/test_ssl_bootstrap.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add windows/__init__.py windows/ssl_bootstrap.py tests/windows/test_ssl_bootstrap.py
git commit -m "feat(windows): self-signed TLS bootstrap for the desktop launcher"
```

---

### Task 7: Launcher + default.env

**Files:**
- Create: `windows/launcher.py`
- Create: `windows/default.env`
- Test: `tests/windows/test_launcher.py`

**Interfaces:**
- Consumes: `windows.ssl_bootstrap.ensure_self_signed`, `server.core.paths.app_root` (for bundled assets).
- Produces: module functions `app_dir()`, `user_data_dir()`, `load_env(path) -> dict[str,str]`, `local_url(env, secure) -> str`, `rigctld_command(env, device_cfg, app) -> list[str]`, `server_command(app, ssl_pair) -> list[str]`, `wait_for_server(url, proc, timeout, secure) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/windows/test_launcher.py`:

```python
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
    assert mod.local_url(env, secure=True) == "https://127.0.0.1:8000"


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
```

Note: `windows/launcher.py` needs to be importable via `importlib` for tests; keep it free of top-level side effects (only `main()` guarded by `if __name__ == "__main__"`). The `_load()` helper below inserts the repo root into `sys.path` so `windows.ssl_bootstrap` and `server.core.paths` imports resolve.

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/windows/test_launcher.py -v`
Expected: FAIL — file not found / function not defined.

- [ ] **Step 3: Implement `windows/launcher.py`**

```python
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
```

- [ ] **Step 4: Create `windows/default.env`**

```ini
# MRRC_FT8 Windows default configuration (copied to %LOCALAPPDATA%\MRRC-FT8\ft8.env on first run).
MRRC_FT8_PASSWORD_HASH=$argon2id$v=19$m=65536,t=3,p=4$O9MnaIl35WfL9suCNKNmow$GGG/Wk2+zS7RX8KGUUXd2+2/k8wrGW5eA7a+pU7PAWU
MRRC_FT8_MY_CALL=N0CALL
MRRC_FT8_MY_GRID=AA00AA
MRRC_FT8_WEB_HOST=0.0.0.0
MRRC_FT8_WEB_PORT=8000
MRRC_FT8_RIGCTLD=127.0.0.1:4532
# Audio device names are matched against the launcher's startup device list;
# use the name printed there, or leave blank to use the system default.
#MRRC_FT8_AUDIO_DEVICE=
#MRRC_FT8_DECODER_PROFILE=3
#MRRC_FT8_DECODER_THREADS=auto
#MRRC_FT8_ALLOWED_HOSTS=localhost,<hostname>,<lan-ip>
```

The `MRRC_FT8_ALLOWED_HOSTS` default is computed by the launcher (`load_env`) to include the machine hostname + LAN IPs; set it explicitly to override. Change the default callsign/grid and the password hash before shipping.

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/windows/test_launcher.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add windows/launcher.py windows/default.env tests/windows/test_launcher.py
git commit -m "feat(windows): MRRC_FT8 desktop launcher + default.env"
```

---

### Task 8: PyInstaller specs + build requirements + .gitignore

**Files:**
- Create: `packaging/windows/ft8_server.spec`
- Create: `packaging/windows/ft8_launcher.spec`
- Create: `packaging/windows/requirements-build.txt`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: built `desktop/ft8web/dist`, `vendor/wsjtx-runtime/windows/x64/wsjt_core.dll` + runtime DLLs, `vendor/hamlib/windows/x64/rigctld.exe`, `windows/restart.ps1`.
- Produces: `ft8-server.exe` (onedir, `_internal/` layout), `MRRC_FT8.exe` (launcher onefile).

- [ ] **Step 1: Create `packaging/windows/ft8_server.spec`**

```python
# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the MRRC_FT8 server (onedir). Build on Windows with
# packaging/windows/build.ps1 after: tests, desktop/client npm build, and the
# vendor DLLs under vendor/wsjtx-runtime + vendor/hamlib are in place.
from pathlib import Path
import sys

ROOT = Path(SPECPATH).parents[2]
DIST_ROOT = ROOT / "dist" / "windows" / "_pyinstaller"

_extra_data = []
_dll_root = ROOT / "vendor" / "wsjtx-runtime" / "windows" / "x64"
if _dll_root.exists():
    _extra_data.append((str(_dll_root), "."))  # wsjt_core.dll + libgfortran/libgomp/libfftw3f...
_hamlib_root = ROOT / "vendor" / "hamlib" / "windows" / "x64"
if _hamlib_root.exists():
    _extra_data.append((str(_hamlib_root), "hamlib"))  # rigctld.exe + libhamlib

a = Analysis(
    [str(ROOT / "server" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "server" / "web" / "static"), "server/web/static"),
        (str(ROOT / "desktop" / "ft8web" / "dist"), "desktop/ft8web/dist"),
        (str(ROOT / "cty.dat"), "."),
        (str(ROOT / "windows" / "restart.ps1"), "."),
        *_extra_data,
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "dotenv",
        "serial",
        "serial.tools.list_ports",
        "argon2",
        "argon2.low_level",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ft8-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=True, upx_exclude=[],
    name="ft8-server",
)
```

- [ ] **Step 2: Create `packaging/windows/ft8_launcher.spec`**

```python
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

ROOT = Path(SPECPATH).parents[2]

a = Analysis(
    [str(ROOT / "windows" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "windows" / "default.env"), "windows"),
    ],
    hiddenimports=["cryptography"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="MRRC_FT8",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
```

- [ ] **Step 3: Create `packaging/windows/requirements-build.txt`**

```
# Build-only dependencies for the Windows installer. PyInstaller 6 changed the
# onedir layout ("_internal/") — do not float this version.
pyinstaller==6.21.0
cryptography>=42.0
pyserial>=3.5
```

- [ ] **Step 4: Add vendor dirs to `.gitignore`**

Append to `.gitignore`:

```gitignore
# Windows installer prebuilt binaries (assembled on the build VM)
vendor/wsjtx-runtime/
vendor/hamlib/
```

- [ ] **Step 5: Verify specs reference existing sources**

Run: `python - <<'EOF'
from pathlib import Path
for p in ["server/main.py", "server/web/static", "cty.dat", "windows/launcher.py", "windows/restart.ps1", "windows/default.env"]:
    assert Path(p).exists(), p
print("all spec source paths exist")
EOF`
Expected: prints "all spec source paths exist".

- [ ] **Step 6: Commit**

```bash
git add packaging/windows/ft8_server.spec packaging/windows/ft8_launcher.spec packaging/windows/requirements-build.txt .gitignore
git commit -m "build(windows): PyInstaller specs for MRRC_FT8 server + launcher"
```

---

### Task 9: Inno Setup installer script

**Files:**
- Create: `packaging/windows/MRRC-FT8.iss`

- [ ] **Step 1: Create `packaging/windows/MRRC-FT8.iss`**

```ini
#define MyAppName "MRRC_FT8"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "MRRC"
#define MyAppExeName "MRRC_FT8.exe"

[Setup]
AppId={{86A45855-A275-49EE-A8B1-BB0009CF1BF3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\MRRC_FT8
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\windows
OutputBaseFilename=MRRC_FT8-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin

[Files]
Source: "..\..\dist\windows\MRRC_FT8\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Edit Configuration"; Filename: "notepad.exe"; Parameters: """{localappdata}\MRRC-FT8\ft8.env"""
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""MRRC_FT8 Web"" dir=in action=allow protocol=TCP localport=8000"; Flags: runhidden; StatusMsg: "Opening firewall port 8000..."
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
```

- [ ] **Step 2: Verify the AppId parses**

The AppId `{86A45855-A275-49EE-A8B1-BB0009CF1BF3}` is a valid GUID (freshly generated, distinct from mrrc_ft710's). Keep the Inno Setup `{{` `}` doubling exactly as shown. If a later release wants a fresh identity, regenerate with `[guid]::NewGuid()` and re-check only that it is not already used.

- [ ] **Step 3: Commit**

```bash
git add packaging/windows/MRRC-FT8.iss
git commit -m "build(windows): Inno Setup script for MRRC_FT8-Setup.exe"
```

---

### Task 10: build.ps1 orchestration

**Files:**
- Create: `packaging/windows/build.ps1`

- [ ] **Step 1: Create `packaging/windows/build.ps1`**

```powershell
$ErrorActionPreference = "Stop"

# $ErrorActionPreference does NOT apply to native commands — check
# $LASTEXITCODE explicitly so a failing test or build aborts packaging.
function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$Command,
        [ValueFromRemainingArguments = $true]$Remaining
    )
    $flat = @()
    foreach ($a in $Remaining) { $flat += $a }
    & $Command @flat
    if ($LASTEXITCODE -ne 0) {
        throw "$Command $($flat -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$DistRoot = Join-Path $RepoRoot "dist\windows"
$AppRoot = Join-Path $DistRoot "MRRC_FT8"
$PyInstallerRoot = Join-Path $DistRoot "_pyinstaller"
$DllDir = Join-Path $RepoRoot "vendor\wsjtx-runtime\windows\x64"

Set-Location $RepoRoot

# 1. tests (must be green before packaging)
Invoke-Checked python -m pytest tests/ -q

# 2. desktop client build (dist/ is gitignored)
Push-Location (Join-Path $RepoRoot "desktop\ft8web")
Invoke-Checked npm ci
Invoke-Checked npm run build
Pop-Location

# 3. sanity-check vendored binaries (mirrors mrrc_ft710's FTDI warning)
if (!(Test-Path (Join-Path $DllDir "wsjt_core.dll"))) {
    Write-Warning "wsjt_core.dll missing. Build it on the VM once (see win_pack.md) and copy it to:"
    Write-Warning "  $DllDir"
}
$rigExe = Join-Path $RepoRoot "vendor\hamlib\windows\x64\rigctld.exe"
if (!(Test-Path $rigExe)) {
    Write-Warning "rigctld.exe missing. Download the Hamlib Windows build to:"
    Write-Warning "  $rigExe"
}

# 4. PyInstaller
Invoke-Checked pyinstaller packaging\windows\ft8_server.spec --noconfirm --distpath "$PyInstallerRoot" --workpath "build\pyinstaller"
Invoke-Checked pyinstaller packaging\windows\ft8_launcher.spec --noconfirm --distpath "$PyInstallerRoot" --workpath "build\pyinstaller"

# 5. assemble the app directory
if (Test-Path $AppRoot) { Remove-Item $AppRoot -Recurse -Force }
New-Item -ItemType Directory -Path $AppRoot | Out-Null
Copy-Item (Join-Path $PyInstallerRoot "ft8-server\*") $AppRoot -Recurse -Force
Copy-Item (Join-Path $PyInstallerRoot "MRRC_FT8.exe") $AppRoot -Force
Copy-Item (Join-Path $RepoRoot "windows") $AppRoot -Recurse -Force
Remove-Item (Join-Path $AppRoot "windows\__pycache__") -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path (Join-Path $RepoRoot "vendor\hamlib\windows\x64")) {
    $dest = Join-Path $AppRoot "hamlib"
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Copy-Item (Join-Path $RepoRoot "vendor\hamlib\windows\x64\*") $dest -Recurse -Force
}

# 6. Inno Setup
if (Get-Command iscc -ErrorAction SilentlyContinue) {
    Invoke-Checked iscc packaging\windows\MRRC-FT8.iss
} else {
    Write-Warning "Inno Setup Compiler 'iscc' not found. Install Inno Setup and rerun to create the Setup EXE."
}

Write-Host "Installer output: $(Join-Path $DistRoot 'MRRC_FT8-Setup.exe')"
```

- [ ] **Step 2: Commit**

```bash
git add packaging/windows/build.ps1
git commit -m "build(windows): MRRC_FT8 packaging orchestration (tests -> npm -> pyinstaller -> iscc)"
```

---

### Task 11: Documentation (packager manual + user guide)

**Files:**
- Create: `win_pack.md`
- Create: `docs/WINDOWS_INSTALLER_GUIDE.md`

- [x] **Step 1: Write `win_pack.md`** — mirror the structure of `mrrc_ft710/win_pack.md`:

- §1 Environment topology (Mac → ham.vlsc.net → Win11 VM `192.168.122.133`; VM: Python 3.12, Inno Setup, msys2/MinGW, Node.js).
- §2 One-time VM setup: SSH key into VM (`C:\ProgramData\ssh\administrators_authorized_keys` + icacls); msys2 install + `pacman -S mingw-w64-x86_64-gcc-fortran mingw-w64-x86_64-cmake mingw-w64-x86_64-fftw`; Node.js; `venv` + `pip install -r requirements.txt -r packaging/windows/requirements-build.txt`; official WSJT-X 3.0.2 Windows install → copy `C:\Program Files\WSJT\bin\{libgfortran-5,libgomp-1,libfftw3f-3,libquadmath-0,libwinpthread-1}.dll` to `vendor/wsjtx-runtime/windows/x64/`; Hamlib Windows build → `vendor/hamlib/windows/x64/`; **DSP build once**: in an msys2 mingw64 shell `cmake -S dsp -B dsp/build -G "MinGW Makefiles" && cmake --build dsp/build -j` then copy `dsp/build/wsjt_core.dll` to `vendor/wsjtx-runtime/windows/x64/`.
- §3 Per-release flow: local `pytest`; source zip (exclude `.git`, `venv`, `dist`, `build`, `vendor/`, `*.pyc`, `wsjtx-3.0.2/`, `mrrc-ft8.db`); scp to VM; unzip; `build.ps1`; verify artifacts (Setup.exe, `_internal` layout, DLLs); scp back to Mac `dist/windows/`.
- §4 Troubleshooting table (mirror mrrc_ft710's: SSH admin key ACL, PowerShell `;` vs `&&`, GBK encoding, PyInstaller `_internal` path gotchas, `OMP_STACKSIZE`, mp spawn + `freeze_support`, firewall rule, self-signed cert warning).
- §5 VM command cheat-sheet.

- [x] **Step 2: Write `docs/WINDOWS_INSTALLER_GUIDE.md`** — user-facing:

- Install `MRRC_FT8-Setup.exe` (Windows 11/10 x64); admin for the firewall rule.
- First run: launcher copies config to `%LOCALAPPDATA%\MRRC-FT8\ft8.env`, generates a self-signed cert, starts rigctld + server, opens `https://localhost:8000/desktop/` — accept the browser's one-time certificate warning.
- Login password: `abcd1234` (change it: edit `ft8.env` → set `MRRC_FT8_PASSWORD_HASH` to the output of `python -m server.main --hash-password NEWPASS` on the same machine; or from the packaged app folder run `.\python` if bundled).
- Configure radio + audio via **Settings → Devices** in the UI (`/api/v1/devices`): rig model/COM port/baud, audio in/out, then **Apply** (restarts the stack via `restart.ps1`).
- LAN/remote: connect from another device to `https://<pc-hostname-or-ip>:8000/desktop/`; the cert is self-signed, so accept the warning once. Ensure the firewall rule for TCP 8000 exists.
- Known limits: KVM-virtualised audio out is not reliable for TX verification — use a physical Windows machine for TX audio checks.

- [x] **Step 3: Commit**

```bash
git add win_pack.md docs/WINDOWS_INSTALLER_GUIDE.md
git commit -m "docs(windows): packaging manual + Windows installer user guide"
```

---

### Task 12: VM build + smoke verification (checklist, partially manual)

**Files:** none (verification only).

- [ ] **Step 1: Push repo to the VM and set up the environment**

Follow `win_pack.md` §2 (SSH key, msys2, Node, venv, PyInstaller, WSJT-X runtime DLLs, Hamlib, DSP build). This is a one-time manual step documented in Task 11.

- [ ] **Step 2: Build the installer**

Run `packaging/windows/build.ps1` on the VM (`build_vm.ps1` wrapper as in mrrc_ft710). Expected: pytest green → npm build → PyInstaller ×2 → iscc → `dist\windows\MRRC_FT8-Setup.exe`.

- [ ] **Step 3: Verify the artifact layout**

```powershell
dir dist\windows\MRRC_FT8\MRRC_FT8.exe
dir dist\windows\MRRC_FT8\ft8-server.exe
dir dist\windows\MRRC_FT8\_internal\wsjt_core.dll
dir dist\windows\MRRC_FT8\_internal\server\web\static\index.html
dir dist\windows\MRRC_FT8\_internal\desktop\ft8web\dist\index.html
dir dist\windows\MRRC_FT8\_internal\cty.dat
dir dist\windows\MRRC_FT8\hamlib\rigctld.exe
Get-FileHash dist\windows\MRRC_FT8-Setup.exe -Algorithm SHA256
```

- [ ] **Step 4: Install + smoke test**

Install the Setup.exe on the VM; launch MRRC_FT8.exe; confirm:
- `%LOCALAPPDATA%\MRRC-FT8\ft8.env` created with the baked password hash;
- `https://localhost:8000/desktop/` loads after accepting the cert warning;
- login with `abcd1234` succeeds;
- `https://<vm-lan-ip>:8000/desktop/` loads from the host (LAN access + allowed-hosts + firewall rule all verified);
- `/api/health` reports radio/audio green (or radio red when no rig is attached — expected on the VM);
- device-config apply (`/api/v1/devices/apply`) restarts the stack via `restart.ps1`.

- [ ] **Step 5: Fetch the installer back to the Mac**

```bash
ssh ham.vlsc.net "scp cheenle@192.168.122.133:C:/.../dist/windows/MRRC_FT8-Setup.exe /tmp/MRRC_FT8-Setup.exe"
scp ham.vlsc.net:/tmp/MRRC_FT8-Setup.exe dist/windows/
shasum -a 256 dist/windows/MRRC_FT8-Setup.exe
```

---

## Self-Review Notes

- **Spec coverage:** every design section maps to a task — server host/port/SSL (T1), frozen paths (T2), Windows serial (T3), restart (T4), DSP .dll + freeze_support (T5), ssl_bootstrap (T6), launcher + default.env + password hash (T7), PyInstaller (T8), Inno Setup (T9), build.ps1 (T10), docs (T11), VM verification (T12). The `MRRC_FT8_ALLOWED_HOSTS` LAN fix is in the launcher (T7 `load_env`).
- **Placeholders:** none. The Inno AppId is a real generated GUID (`{86A45855-A275-49EE-A8B1-BB0009CF1BF3}`).
- **Type consistency:** `ensure_self_signed` returns `tuple[Path, Path] | None` (T6) and is consumed that way in `server_command`/`main` (T7); `app_root()` (T2) is consumed by `default_library_path` (T5), `_static_dir`/`_desktop_dist_dir` (T2), and `DeviceConfigStore` (T4); `rigctld_command(env, device_cfg, app)` signature is identical in launcher and restart.ps1 arg build. The launcher's `server_command` emits only `--ssl-cert/--ssl-key` (Task 1 defines no `--no-ssl` flag).
