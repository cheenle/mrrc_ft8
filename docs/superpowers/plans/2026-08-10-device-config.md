# 设备配置可配置化（HAMLIB 电台 + 音频设备）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在桌面客户端（Settings 弹窗）与手机 PWA（☰ 抽屉 Devices 标签页）中配置电台（HAMLIB：rig 型号/串口/波特率/rigctld 端口）与音频设备；「保存」落盘不重启，「应用重启」自动重启 rigctld + server。

**架构：** 新增 `data/device-config.json`（gitignore 已覆盖 `data/`）作为设备配置唯一落盘点。server 启动时合并（audio_device + rigctld_port 覆盖 env）；`restart.sh` 拉起 rigctld 时读它。REST `/api/v1/devices`（GET/PUT/POST apply）负责读写与触发重启（`setsid` 分离 spawn restart.sh，杀 server 不影响脚本）。串口唯一 owner 仍是 rigctld（AD-008）。

**技术栈：** Python 3.12 + FastAPI（asyncio、`asyncio.to_thread`）、vanilla JS（PWA，无构建）、React 19 + Vite + vitest（桌面）、bash（restart.sh）、pytest。

**规格：** `docs/superpowers/specs/2026-08-10-device-config-design.md`

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `server/engine/device_config.py` | 创建 | 纯逻辑：load/save（原子写）/merge/enumerate/validate/source/shell_env_lines/DeviceConfigStore |
| `server/engine/rig.py` | 修改 | 加 `host`/`port` 只读属性（API 报告 rig 连接状态用） |
| `server/main.py` | 修改 | `main()` 里 `ServerConfig.from_env()` 后合并文件覆盖；`create_server` 里 `state.device_config = DeviceConfigStore()` |
| `server/web/api.py` | 修改 | `AppState` 加 `device_config` 字段；`GET/PUT /devices` + `POST /devices/apply` |
| `restart.sh` | 修改 | 顶部：JSON 存在 → eval python 输出的 `KEY=value` 覆盖 RIG_* |
| `desktop/ft8web/src/services/mrrcClient.ts` | 修改 | 加 `devices()/saveDevices()/applyDevices()` |
| `desktop/ft8web/src/components/DeviceSettings.tsx` | 创建 | Devices 表单组件 + 纯 helper（可单测） |
| `desktop/ft8web/src/components/deviceConfig.test.ts` | 创建 | helper vitest |
| `desktop/ft8web/src/App.tsx` | 修改 | 设置弹窗顶部嵌入 `<DeviceSettings/>`；openSettingsModal 拉 devices；save/apply 回调 |
| `desktop/ft8web/src/services/mrrcClient.test.ts` | 修改 | 新方法冒烟测试 |
| `server/web/static/index.html` | 修改 | drawer-tabs 加 `<button data-tab="devices">Devices</button>` |
| `server/web/static/js/api.js` | 修改 | 加 `devices()/saveDevices()/applyDevices()` |
| `server/web/static/js/settings.js` | 修改 | `renderTab` 加分支 + `renderDevices()` |
| `tests/engine/test_device_config.py` | 创建 | 纯逻辑单测（tmp_path） |
| `tests/web/test_devices.py` | 创建 | API 端点测试（仿 `test_api.py` fixture 风格） |
| `tests/web/test_static.py` | 修改 | 断言 index.html 含 Devices 标签 |
| `tests/README.md`、`SDD/14-version-history.md`、`AGENTS.md`、`SDD/12-…`（配置章节）、`SDD/08-architecture-decisions.md`（AD-008 注记） | 修改 | 文档同步 |

---

### Task 1：`server/engine/device_config.py` 纯逻辑模块（TDD）

**文件：**
- 创建：`server/engine/device_config.py`
- 测试：`tests/engine/test_device_config.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/engine/test_device_config.py
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
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    assert enumerate_audio_devices() == []


def test_enumerate_serial_devices(monkeypatch) -> None:
    monkeypatch.setattr(
        "server.engine.device_config.glob.glob",
        lambda pat: [f"/dev/cu.usbserial-0121DB3A0"] if "cu." in pat else [],
    )
    assert enumerate_serial_devices() == ["/dev/cu.usbserial-0121DB3A0"]


def test_validate_accepts_good_config() -> None:
    cfg = {"rig_model": 1049, "rig_device": "/dev/cu.x", "rig_baud": 38400,
           "rigctld_port": 4532, "audio_device": "USB"}
    assert validate(cfg, [{"index": 0, "name": "USB"}]) is None


@pytest.mark.parametrize("bad", [
    {"rig_model": 0},
    {"rig_model": "x"},
    {"rig_device": "usb"},
    {"rig_baud": 12345},
    {"rigctld_port": 80},
    {"rigctld_port": 8000},
    {"audio_device": "Not-There"},
])
def test_validate_rejects(bad: dict) -> None:
    assert validate(bad, [{"index": 0, "name": "USB"}]) is not None


def test_validate_allows_unchanged_audio_even_when_absent() -> None:
    assert validate({"audio_device": "USB"}, [], current_effective="USB") is None


def test_source_of_priority(monkeypatch) -> None:
    env = {"MRRC_FT8_AUDIO_DEVICE": "USB", "MRRC_FT8_RIG_MODEL": "1049"}
    src = source_of({"audio_device": "Other"}, environ=env)
    assert src["audio_device"] == "file"
    assert src["rig_model"] == "env"
    assert src["rigctld_port"] == "default"


def test_store_roundtrip_and_spawn_script(tmp_path) -> None:
    store = DeviceConfigStore(tmp_path / "d.json", script="/nonexistent/restart.sh")
    store.save({"rig_model": 3073})
    assert store.load() == {"rig_model": 3073}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/engine/test_device_config.py -v`
预期：FAIL，`ModuleNotFoundError: No module named 'server.engine.device_config'`

- [ ] **步骤 3：编写最少实现**

```python
# server/engine/device_config.py
"""Station device configuration (HAMLIB rig + audio) — spec 2026-08-10, AD-008.

``data/device-config.json`` is the operator-editable station device config:
written by the ``/api/v1/devices`` endpoints, read at startup by the server
(audio device + rigctld port) and by ``restart.sh`` (rigctld launch params).
env vars remain the fallback when the file is absent, so existing deployments
keep working unchanged.  ``restart.sh`` stays the rigctld spawner (AD-008:
rigctld is the serial owner); this module never opens the serial device.
"""

from __future__ import annotations

import dataclasses
import glob
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("mrrc-ft8.device-config")

DEFAULT_CONFIG_PATH = Path("data/device-config.json")

# Curated hamlib rig models offered in the UI (rigctl -l, verified 2026-08-10).
CURATED_RIG_MODELS: list[tuple[int, str]] = [
    (1020, "Yaesu FT-817"),
    (1049, "Yaesu FT-710"),
    (3073, "Icom IC-7300"),
    (30003, "Icom IC-M710"),
]

BAUD_RATES: list[int] = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]

_CONFIG_KEYS = ("rig_model", "rig_device", "rig_baud", "rigctld_port", "audio_device")

_ENV_KEY = {
    "rig_model": "MRRC_FT8_RIG_MODEL",
    "rig_device": "MRRC_FT8_RIG_DEVICE",
    "rig_baud": "MRRC_FT8_RIG_BAUD",
    "rigctld_port": "MRRC_FT8_RIGCTLD_PORT",
    "audio_device": "MRRC_FT8_AUDIO_DEVICE",
}

# source_of 的 env 检测用 server 侧语义：rigctld_port 的 server 连接变量是
# ``MRRC_FT8_RIGCTLD``（host:port），restart.sh 拉起变量是
# ``MRRC_FT8_RIGCTLD_PORT``（仅端口）——两者用途不同，分开维护。
_SOURCE_ENV = {
    "rig_model": "MRRC_FT8_RIG_MODEL",
    "rig_device": "MRRC_FT8_RIG_DEVICE",
    "rig_baud": "MRRC_FT8_RIG_BAUD",
    "rigctld_port": "MRRC_FT8_RIGCTLD",  # server 实际连接所用 env（host:port）
    "audio_device": "MRRC_FT8_AUDIO_DEVICE",
}


def load_device_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any] | None:
    """Return the saved device config, or None when absent/corrupt."""

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: data[k] for k in _CONFIG_KEYS if k in data}


def save_device_config(cfg: dict[str, Any], path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    """Atomic write (tmp + os.replace); only known keys are persisted."""

    payload = {k: cfg[k] for k in _CONFIG_KEYS if k in cfg and cfg[k] is not None}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="device-config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def shell_env_lines(cfg: dict[str, Any]) -> list[str]:
    """``KEY=value`` lines for restart.sh to ``eval`` (rigctld launch params)."""

    lines: list[str] = []
    for key, env in _ENV_KEY.items():
        if key in cfg and cfg[key] is not None:
            lines.append(f"{env}={cfg[key]!r}")
    return lines


def effective_config(
    file_cfg: dict[str, Any] | None, environ: Any = os.environ
) -> dict[str, Any]:
    """Effective values the station would use: file > env > default."""

    cfg: dict[str, Any] = {}
    audio_raw = environ.get("MRRC_FT8_AUDIO_DEVICE", "")
    if audio_raw:
        cfg["audio_device"] = int(audio_raw) if audio_raw.isdigit() else audio_raw
    _, _, rig_port = environ.get("MRRC_FT8_RIGCTLD", "127.0.0.1:4532").partition(":")
    cfg["rigctld_port"] = int(rig_port or 4532)
    for env, key in (
        ("MRRC_FT8_RIG_MODEL", "rig_model"),
        ("MRRC_FT8_RIG_DEVICE", "rig_device"),
        ("MRRC_FT8_RIG_BAUD", "rig_baud"),
    ):
        value = environ.get(env, "")
        if value:
            cfg[key] = int(value) if value.isdigit() else value
    if file_cfg:
        cfg.update(file_cfg)
    return cfg


def merge_into(config: Any, file_cfg: dict[str, Any] | None) -> Any:
    """ServerConfig copy with file overrides (audio_device, rigctld_port)."""

    if not file_cfg:
        return config
    overrides: dict[str, Any] = {}
    if "audio_device" in file_cfg:
        overrides["audio_device"] = file_cfg["audio_device"]
    if "rigctld_port" in file_cfg:
        overrides["rigctld_port"] = int(file_cfg["rigctld_port"])
    return dataclasses.replace(config, **overrides)


def enumerate_audio_devices() -> list[dict[str, Any]]:
    """sounddevice devices → [{index, name, max_input, max_output}]; [] on error."""

    try:
        import sounddevice

        devices = sounddevice.query_devices()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for index, dev in enumerate(devices):
        rows.append(
            {
                "index": index,
                "name": str(dev["name"]),
                "max_input": int(dev["max_input_channels"]),
                "max_output": int(dev["max_output_channels"]),
            }
        )
    return rows


def enumerate_serial_devices() -> list[str]:
    """Candidate CAT serial devices (macOS cu.* plus common Linux names)."""

    return sorted(
        set(glob.glob("/dev/cu.*")) | set(glob.glob("/dev/ttyUSB*")) | set(glob.glob("/dev/ttyACM*"))
    )


def validate(
    cfg: dict[str, Any],
    audio_devices: list[dict[str, Any]],
    *,
    current_effective: Any = None,
) -> str | None:
    """Error message for an invalid config, else None."""

    if "rig_model" in cfg:
        model = cfg["rig_model"]
        if not isinstance(model, int) or isinstance(model, bool) or model <= 0:
            return "rig_model must be a positive integer"
    if "rig_device" in cfg:
        device = cfg["rig_device"]
        if not isinstance(device, str) or not device.strip().startswith("/dev/"):
            return "rig_device must be an absolute /dev/... path"
    if "rig_baud" in cfg:
        if cfg["rig_baud"] not in BAUD_RATES:
            return "rig_baud must be one of " + ", ".join(str(b) for b in BAUD_RATES)
    if "rigctld_port" in cfg:
        port = cfg["rigctld_port"]
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535 or port == 8000:
            return "rigctld_port must be an integer in 1024..65535 (not 8000)"
    if "audio_device" in cfg and cfg["audio_device"] is not None:
        wanted = cfg["audio_device"]
        names = {str(d["name"]) for d in audio_devices}
        indexes = {d["index"] for d in audio_devices}
        ok = (isinstance(wanted, str) and wanted in names) or (
            isinstance(wanted, int) and not isinstance(wanted, bool) and wanted in indexes
        )
        if not ok and current_effective is not None and wanted == current_effective:
            ok = True  # unchanged value passes even while the device is absent
        if not ok:
            return "audio_device not found in device list"
    return None


def source_of(file_cfg: dict[str, Any] | None, environ: Any = os.environ) -> dict[str, str]:
    """Per-key origin: file | env | default."""

    source: dict[str, str] = {}
    for key in _CONFIG_KEYS:
        if file_cfg and key in file_cfg:
            source[key] = "file"
        elif environ.get(_SOURCE_ENV[key]):
            source[key] = "env"
        else:
            source[key] = "default"
    return source


class DeviceConfigStore:
    """The API's seam: load/save the config file and spawn the restart script."""

    def __init__(
        self, path: str | Path = DEFAULT_CONFIG_PATH, *, script: str | Path | None = None
    ) -> None:
        self.path = Path(path)
        self.script = Path(script) if script else Path(__file__).resolve().parents[2] / "restart.sh"

    def load(self) -> dict[str, Any] | None:
        return load_device_config(self.path)

    def save(self, cfg: dict[str, Any]) -> None:
        save_device_config(cfg, self.path)

    def spawn_restart(self) -> None:
        """Detached restart.sh run; survives this process being killed."""

        if not self.script.exists():
            raise FileNotFoundError(f"restart script missing: {self.script}")
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

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/engine/test_device_config.py -v`
预期：PASS（`merge_into` 测试用显式 `ServerConfig(...)` 构造，不依赖 `.env`）

- [ ] **步骤 5：Commit**

```bash
git add server/engine/device_config.py tests/engine/test_device_config.py
git commit -m "feat(device-config): station device config module (json store + validation + enumeration)"
```

---

### Task 2：server 启动接线 — `main.py` 合并 + `rig.py` host/port 属性（TDD）

**文件：**
- 修改：`server/main.py`（`main()` 内 `config = ServerConfig.from_env()` 之后、`create_server` 的 AppState 构造处）
- 修改：`server/engine/rig.py`（加属性）
- 测试：`tests/engine/test_rig.py`（属性）+ `tests/web/test_main.py`（合并）

- [ ] **步骤 1：编写失败的测试**

`tests/engine/test_rig.py` 追加：

```python
def test_rig_client_exposes_host_and_port() -> None:
    from server.engine.rig import RigClient

    rig = RigClient(host="127.0.0.2", port=4533)
    assert rig.host == "127.0.0.2"
    assert rig.port == 4533
```

`tests/web/test_main.py` 追加（先确认该文件现有 fixture 模式后追加同风格用例；核心断言如下）：

```python
def test_server_config_merges_device_file(tmp_path) -> None:
    from server.engine.device_config import load_device_config, merge_into, save_device_config
    from server.main import ServerConfig

    path = tmp_path / "device-config.json"
    save_device_config({"audio_device": "USB Audio", "rigctld_port": 4533}, path)
    base = ServerConfig(
        password_hash="h", my_call="M0XX", my_grid="IO91",
        allowed_hosts=frozenset({"localhost"}),
    )
    config = merge_into(base, load_device_config(path))
    assert config.audio_device == "USB Audio"
    assert config.rigctld_port == 4533
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/engine/test_rig.py -v`
预期：FAIL（`RigClient` 无 `host` 属性）

- [ ] **步骤 3：实现**

`server/engine/rig.py`，`connected` 属性之后加：

```python
    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port
```

`server/main.py`：
1. 导入区加：`from .engine.device_config import DeviceConfigStore, load_device_config, merge_into`
2. `main()` 中 `config = ServerConfig.from_env()` 之后：

```python
    config = ServerConfig.from_env()
    device_file = load_device_config()
    if device_file:
        config = merge_into(config, device_file)
        log.info("device config file overrides: %s", sorted(device_file))
```

3. `create_server` 中 AppState 构造处（`band_hunt_url=config.band_hunt_url,` 那一段）加一行：

```python
        band_hunt_url=config.band_hunt_url,
        device_config=DeviceConfigStore(),
```

4. `server/web/api.py` 的 `AppState` 加字段（**计划修正**：原计划归 Task 3，但本任务接线与冒烟即需要它，提前到此；Task 3 勿重复添加）：

```python
    device_config: Any = None  # DeviceConfigStore when wired
```

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/engine/test_rig.py tests/web/test_main.py -v`
预期：PASS

- [ ] **步骤 5：启动冒烟验证接线**

运行：`cd /Users/cheenle/HAM/ft8 && venv/bin/python -c "
from server.engine.device_config import DeviceConfigStore
from server.main import ServerConfig, create_server
app = create_server(ServerConfig.from_env(), rig=None, start_dsp=False, start_audio=False)
assert app.state.app_state.device_config is not None
print('device_config wired:', type(app.state.app_state.device_config).__name__)
"`
预期：打印 `device_config wired: DeviceConfigStore`（`get_state` 读取 `request.app.state.app_state`，故断言用 `app.state.app_state.device_config`）

- [ ] **步骤 6：Commit**

```bash
git add server/main.py server/engine/rig.py tests/engine/test_rig.py tests/web/test_main.py
git commit -m "feat(device-config): merge device file into server config at startup"
```

---

### Task 3：REST API `/api/v1/devices`（TDD）

**文件：**
- 修改：`server/web/api.py`
- 创建：`tests/web/test_devices.py`

前置：仿 `tests/web/test_api.py` 的 fixture（`ApiRig`/`state`/`client`/`login`/`auth_headers`，`acquire_lease` 后 `operation/cq` 置 TX）。测试的 `state` 需注入 `device_config=DeviceConfigStore(tmp_path / "device-config.json")` 与 `rig=ApiRig()`（ApiRig 无 host/port 属性——端点用 `getattr` 兜底，断言含 fallback）。

- [ ] **步骤 1：编写失败的测试**

```python
# tests/web/test_devices.py
from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import PASSWORD, ApiRig, acquire_lease, auth_headers, login

from server.engine.audio_tx import TxPlayer
from server.engine.cq_loop import CqLoopController
from server.engine.device_config import DeviceConfigStore, save_device_config
from server.engine.repository import Repository
from server.engine.safety import SafetyController
from server.engine.sequencer import Sequencer
from server.web.api import COOKIE_NAME, AppState, create_app
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


def test_devices_put_locked_when_ptt_on_only(client, tmp_path) -> None:
    """TX 锁同时检查 safety.ptt_on（非仅 armed）；直接置位验证该分支。"""

    session_id = login(client)
    client.app.state.app_state.safety.ptt_on = True
    res = client.put(
        "/api/v1/devices", json={"rigctld_port": 4533}, headers=auth_headers(session_id)
    )
    assert res.status_code == 409
    assert res.json()["reason"] == "tx_active"


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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/web/test_devices.py -v`
预期：FAIL（404：路由不存在）

- [ ] **步骤 3：实现端点**

`server/web/api.py`：
1. 导入区加：

```python
import subprocess
from pathlib import Path

from .engine.device_config import (
    BAUD_RATES,
    CURATED_RIG_MODELS,
    DeviceConfigStore,
    effective_config,
    enumerate_audio_devices,
    enumerate_serial_devices,
    load_device_config,
    save_device_config,
    source_of,
    validate,
)
```

2. `AppState` 加字段（`band_hunt_url` 之后）：

```python
    device_config: Any = None  # DeviceConfigStore when wired
```

> **注意：** 若 Task 2 已提前添加该字段（计划修正），此处跳过，不要重复。

3. settings 段之后、`return router` 之前加：

```python
    # ---- station device configuration (spec 2026-08-10) ----------------------

    @router.get("/devices")
    async def devices_view(session: Session = Depends(require_session)) -> JSONResponse:
        store: DeviceConfigStore | None = state.device_config
        file_cfg = store.load() if store else None
        audio = await asyncio.to_thread(enumerate_audio_devices)
        serial = await asyncio.to_thread(enumerate_serial_devices)
        return JSONResponse(
            {
                "config": effective_config(file_cfg),
                "source": source_of(file_cfg),
                "audio_devices": audio,
                "serial_devices": serial,
                "rig_status": {
                    "host": getattr(state.rig, "host", "127.0.0.1"),
                    "port": getattr(state.rig, "port", 4532),
                    "connected": bool(getattr(state.rig, "connected", False)),
                },
                "curated_rig_models": [
                    {"model": m, "name": n} for m, n in CURATED_RIG_MODELS
                ],
                "baud_rates": BAUD_RATES,
            }
        )

    @router.put("/devices")
    async def devices_update(
        request: Request, session: Session = Depends(require_session)
    ) -> JSONResponse:
        await validate_mutation(request)
        store: DeviceConfigStore | None = state.device_config
        if store is None:
            return _reject(503, "device_config_unavailable")
        if state.safety.armed or state.safety.ptt_on:
            return _reject(409, REASON_TX_ACTIVE)
        body = await request.json()
        if not isinstance(body, dict) or not body:
            return _reject(422, "invalid_request")
        audio = await asyncio.to_thread(enumerate_audio_devices)
        effective = effective_config(store.load())
        error = await asyncio.to_thread(
            validate, body, audio, current_effective=effective.get("audio_device")
        )
        if error:
            return _reject(422, "invalid_device_config", detail=error)
        await asyncio.to_thread(store.save, body)
        return await mutate(
            request, request.headers.get("idempotency-key"), 200, {"saved": True}
        )

    @router.post("/devices/apply")
    async def devices_apply(
        request: Request, session: Session = Depends(require_session)
    ) -> JSONResponse:
        await validate_mutation(request)
        store: DeviceConfigStore | None = state.device_config
        if store is None or store.load() is None:
            return _reject(409, "no_device_config")
        if state.safety.armed or state.safety.ptt_on:
            return _reject(409, REASON_TX_ACTIVE)
        # The 202 response is guaranteed to reach the client: spawn_restart
        # uses a detached Popen (new session, non-blocking), and restart.sh's
        # own lsof/pgrep discovery runs only after the fork — far later than
        # the ASGI response write for this handler.  No extra sleep needed.
        try:
            await asyncio.to_thread(store.spawn_restart)
        except FileNotFoundError as exc:
            return _reject(503, "restart_script_missing", detail=str(exc))
        return await mutate(
            request,
            request.headers.get("idempotency-key"),
            202,
            {"restarting": True, "eta_s": 20},
        )
```

（`_reject(status, reason, **extra)` 已支持 extra 字段；`mutate` 已存在于 api.py——确认其签名与 settings PUT 用法一致，直接复用。）

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/web/test_devices.py tests/web/test_api.py tests/web/test_auth.py tests/web/test_lease.py -v`
预期：PASS（既有 web 测试不回归）

- [ ] **步骤 5：Commit**

```bash
git add server/web/api.py tests/web/test_devices.py
git commit -m "feat(api): station device config endpoints (GET/PUT/apply)"
```

---

### Task 4：`restart.sh` 读取设备配置（无单测，人工验证）

**文件：**
- 修改：`restart.sh`

- [ ] **步骤 1：修改顶部解析段**

在 `RIG_MODEL="${MRRC_FT8_RIG_MODEL:-1049}"` 三行之前插入：

```bash
# data/device-config.json（server UI 可写，spec 2026-08-10）存在时覆盖
# rigctld 拉起参数；env 仍是回退。rigctld 仍为串口唯一 owner（AD-008）。
if [ -f "data/device-config.json" ]; then
    eval "$(venv/bin/python -c '
import json, pathlib
cfg = json.loads(pathlib.Path("data/device-config.json").read_text())
for env, key in (("MRRC_FT8_RIG_MODEL", "rig_model"),
                 ("MRRC_FT8_RIG_DEVICE", "rig_device"),
                 ("MRRC_FT8_RIG_BAUD", "rig_baud"),
                 ("MRRC_FT8_RIGCTLD_PORT", "rigctld_port")):
    v = cfg.get(key)
    if v is not None:
        print(f"{env}={v!r}")
')"
fi
```

- [ ] **步骤 2：bash 语法校验 + 解析冒烟（不真正重启）**

运行：
```bash
bash -n restart.sh && echo "syntax OK"
venv/bin/python -c "
from server.engine.device_config import save_device_config, shell_env_lines
cfg = {'rig_model': 1049, 'rig_device': '/dev/cu.x', 'rig_baud': 38400, 'rigctld_port': 4532}
save_device_config(cfg, 'data/device-config.json')
print('\n'.join(shell_env_lines(cfg)))
"
```
预期：`syntax OK` + 四行 `KEY=value`。随后人工验证 restart.sh 的 eval 段真实生效（不杀进程）：用与 restart.sh 相同的 python 片段生成 env 文件并 source：
```bash
venv/bin/python -c "import json,pathlib;cfg=json.loads(pathlib.Path('data/device-config.json').read_text());print('MRRC_FT8_RIG_MODEL=%s' % cfg['rig_model'])" > /tmp/mrrc-rig.env
bash -c 'source /tmp/mrrc-rig.env && echo "model=$MRRC_FT8_RIG_MODEL"'
```
预期：`model=1049`

- [ ] **步骤 3：清理冒烟产物 + Commit**

```bash
rm -f data/device-config.json
git add restart.sh
git commit -m "feat(deploy): restart.sh reads data/device-config.json for rigctld params"
```

---

### Task 5：桌面客户端（mrrcClient + DeviceSettings 组件 + App 接线，vitest）

**文件：**
- 修改：`desktop/ft8web/src/services/mrrcClient.ts`
- 修改：`desktop/ft8web/src/services/mrrcClient.test.ts`
- 创建：`desktop/ft8web/src/components/DeviceSettings.tsx`
- 创建：`desktop/ft8web/src/components/deviceConfig.test.ts`
- 修改：`desktop/ft8web/src/App.tsx`

- [ ] **步骤 1：mrrcClient 方法 + 测试**

`mrrcClient.ts` 末尾（`putSetting` 之后）加：

```ts
  devices: () => request('/devices'),
  saveDevices: (cfg: Record<string, unknown>) =>
    request('/devices', { method: 'PUT', idempotencyKey: key(), body: cfg }),
  applyDevices: () =>
    request('/devices/apply', { method: 'POST', idempotencyKey: key() }),
```

`mrrcClient.test.ts` 追加：

```ts
  it('saveDevices sends a PUT with an idempotency key', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { ok: true, saved: true }));
    await mrrc.saveDevices({ rig_model: 1049 });
    const [url, init] = (globalThis.fetch as any).mock.calls[0];
    expect(url).toBe('/api/v1/devices');
    expect(init.method).toBe('PUT');
    expect(init.headers['idempotency-key']).toBeTruthy();
    expect(JSON.parse(init.body).rig_model).toBe(1049);
  });

  it('applyDevices posts to /devices/apply', async () => {
    vi.stubGlobal('fetch', mockFetch(202, { ok: true, restarting: true }));
    await mrrc.applyDevices();
    const [url, init] = (globalThis.fetch as any).mock.calls[0];
    expect(url).toBe('/api/v1/devices/apply');
    expect(init.method).toBe('POST');
  });
```

- [ ] **步骤 2：运行测试验证失败/通过**

运行：`cd desktop/ft8web && npx vitest run src/services/mrrcClient.test.ts`
预期：PASS（方法先于测试一起提交；若想严格 TDD 可先只写测试看到 FAIL——本步直接同提交）

- [ ] **步骤 3：DeviceSettings 组件 + 纯 helper**

```tsx
// desktop/ft8web/src/components/DeviceSettings.tsx
import { useEffect, useRef, useState } from 'react';

export interface DeviceForm {
  rig_model: number;
  rig_device: string;
  rig_baud: number;
  rigctld_port: number;
  audio_device: string | null;
}

export const RIG_MODEL_OPTIONS = [
  { model: 1020, name: 'Yaesu FT-817' },
  { model: 1049, name: 'Yaesu FT-710' },
  { model: 3073, name: 'Icom IC-7300' },
  { model: 30003, name: 'Icom IC-M710' },
];
export const BAUD_OPTIONS = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200];

export function formFromConfig(cfg: Record<string, any> | undefined): DeviceForm {
  return {
    rig_model: typeof cfg?.rig_model === 'number' ? cfg.rig_model : 1049,
    rig_device: typeof cfg?.rig_device === 'string' ? cfg.rig_device : '',
    rig_baud: typeof cfg?.rig_baud === 'number' ? cfg.rig_baud : 38400,
    rigctld_port: typeof cfg?.rigctld_port === 'number' ? cfg.rigctld_port : 4532,
    audio_device: cfg?.audio_device ?? null,
  };
}

export function isCustomModel(form: DeviceForm): boolean {
  return !RIG_MODEL_OPTIONS.some(o => o.model === form.rig_model);
}

export function sourceLabel(source: Record<string, string> | undefined, key: string): string {
  return source?.[key] ?? 'default';
}

export interface DeviceSettingsProps {
  config: Record<string, any> | undefined;
  source: Record<string, string> | undefined;
  audioDevices: Array<{ index: number; name: string; max_input: number; max_output: number }>;
  serialDevices: string[];
  busy: boolean;
  onSave: (form: DeviceForm) => Promise<string | null>; // error message, null = ok
  onApply: () => Promise<string | null>;
}

export function DeviceSettings(props: DeviceSettingsProps) {
  const { config, source, audioDevices, serialDevices, busy, onSave, onApply } = props;
  const [form, setForm] = useState<DeviceForm>(() => formFromConfig(config));
  const [customModel, setCustomModel] = useState(() => String(form.rig_model));
  const [customDevice, setCustomDevice] = useState('');
  const [modelIsCustom, setModelIsCustom] = useState(() => isCustomModel(form));
  const [deviceIsCustom, setDeviceIsCustom] = useState(() =>
    !!(form.rig_device && !serialDevices.includes(form.rig_device)),
  );
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [restarting, setRestarting] = useState(false);
  // The form last saved to the server: a config refresh triggered by the save
  // itself must not reset `saved` (otherwise Apply is permanently unreachable).
  const savedFormRef = useRef<DeviceForm | null>(null);

  useEffect(() => {
    const next = formFromConfig(config);
    setForm(next);
    setCustomModel(String(next.rig_model));
    setModelIsCustom(isCustomModel(next));
    setDeviceIsCustom(!!(next.rig_device && !serialDevices.includes(next.rig_device)));
    const s = savedFormRef.current;
    setSaved(Boolean(s) && JSON.stringify(s) === JSON.stringify(next));
  }, [config]);

  const effectiveCustomDevice = deviceIsCustom ? customDevice : '';

  const set = (patch: Partial<DeviceForm>) => setForm(f => ({ ...f, ...patch }));

  const handleSave = async () => {
    setError(null);
    const next: DeviceForm = { ...form };
    if (modelIsCustom) {
      const n = Number(customModel);
      if (!Number.isInteger(n) || n <= 0) { setError('Custom rig model must be a positive integer'); return; }
      next.rig_model = n;
    }
    if (deviceIsCustom) {
      if (!effectiveCustomDevice.trim().startsWith('/dev/')) {
        setError('Custom serial device must be an absolute /dev/... path');
        return;
      }
      next.rig_device = effectiveCustomDevice;
    }
    const err = await onSave(next);
    if (err) setError(err);
    else { savedFormRef.current = next; setSaved(true); }
  };

  const handleApply = async () => {
    if (!saved) { setError('Save the device settings before applying'); return; }
    if (!window.confirm('Applying will restart rigctld and the server.\n' +
        'Connection drops for about 20 seconds and you must log in again.\nContinue?')) return;
    setError(null);
    setRestarting(true);
    const err = await onApply();
    if (err) { setError(err); setRestarting(false); }
  };

  const selectCls = 'bg-app border border-border-input text-text-main rounded px-3 py-2 text-xs font-mono w-full focus:outline-none focus:border-[#4caf50]';

  return (
    <div className="flex flex-col gap-3 pt-2 border-t border-border-subtle">
      <label className="text-[10px] uppercase tracking-widest text-text-muted">Devices</label>

      <div className="flex flex-col gap-1">
        <label className="text-[10px] text-text-muted">Rig Model (hamlib)</label>
        <select
          className={selectCls}
          value={modelIsCustom ? 'custom' : form.rig_model}
          onChange={e => {
            if (e.target.value === 'custom') { setModelIsCustom(true); setCustomModel(String(form.rig_model)); return; }
            setModelIsCustom(false);
            set({ rig_model: Number(e.target.value) });
          }}
          disabled={busy}
        >
          {RIG_MODEL_OPTIONS.map(o => (
            <option key={o.model} value={o.model}>{o.name} ({o.model})</option>
          ))}
          <option value="custom">Custom…</option>
        </select>
        {modelIsCustom && (
          <input
            type="number" min={1} className={selectCls}
            value={customModel}
            onChange={e => setCustomModel(e.target.value)}
          />
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-[10px] text-text-muted">CAT Serial Device</label>
        <select
          className={selectCls}
          value={deviceIsCustom ? 'custom' : form.rig_device}
          onChange={e => {
            if (e.target.value === 'custom') {
              setDeviceIsCustom(true);
              setCustomDevice(form.rig_device);
              return;
            }
            setDeviceIsCustom(false);
            set({ rig_device: e.target.value });
          }}
          disabled={busy}
        >
          <option value="">—</option>
          {serialDevices.map(d => <option key={d} value={d}>{d}</option>)}
          <option value="custom">Custom…</option>
        </select>
        {deviceIsCustom && (
          <input
            className={selectCls} value={effectiveCustomDevice}
            onChange={e => { setCustomDevice(e.target.value); set({ rig_device: e.target.value }); }}
          />
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-[10px] text-text-muted">Baud Rate</label>
        <select className={selectCls} value={form.rig_baud} disabled={busy}
          onChange={e => set({ rig_baud: Number(e.target.value) })}>
          {BAUD_OPTIONS.map(b => <option key={b} value={b}>{b}</option>)}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-[10px] text-text-muted">rigctld Port</label>
        <input type="number" min={1024} max={65535} className={selectCls}
          value={form.rigctld_port} disabled={busy}
          onChange={e => set({ rigctld_port: Number(e.target.value) })} />
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-[10px] text-text-muted">Audio Device</label>
        <select className={selectCls} value={form.audio_device ?? ''} disabled={busy}
          onChange={e => set({ audio_device: e.target.value || null })}>
          <option value="">System default</option>
          {audioDevices.map(d => (
            <option key={d.index} value={d.name}>{d.name}</option>
          ))}
        </select>
      </div>

      <p className="text-[9px] text-text-muted">
        Source — model: {sourceLabel(source, 'rig_model')} · serial: {sourceLabel(source, 'rig_device')} · baud: {sourceLabel(source, 'rig_baud')} · port: {sourceLabel(source, 'rigctld_port')} · audio: {sourceLabel(source, 'audio_device')}
      </p>

      {restarting && (
        <p className="text-xs text-amber-400" role="status">Restarting… server will disconnect; log in again after it returns.</p>
      )}
      {error && <p className="text-xs text-red-400" role="alert">{error}</p>}

      <div className="flex gap-2 mt-1">
        <button
          onClick={() => void handleSave()} disabled={busy}
          className="bg-app border border-border-input text-text-main rounded px-3 py-1 text-xs font-mono hover:border-[#4caf50]"
        >
          Save
        </button>
        <button
          onClick={() => void handleApply()} disabled={busy}
          className="bg-green-600 hover:bg-green-600 text-black px-3 py-1 rounded text-xs font-bold uppercase tracking-widest"
        >
          Apply & Restart
        </button>
      </div>
    </div>
  );
}
```

- [ ] **步骤 4：helper vitest**

```ts
// desktop/ft8web/src/components/deviceConfig.test.ts
import { describe, expect, it } from 'vitest';
import { BAUD_OPTIONS, RIG_MODEL_OPTIONS, formFromConfig, isCustomModel, sourceLabel } from './DeviceSettings';

describe('DeviceSettings helpers', () => {
  it('offers the curated rig models with hamlib numbers', () => {
    expect(RIG_MODEL_OPTIONS.map(o => o.model)).toEqual([1020, 1049, 3073, 30003]);
  });
  it('defaults a missing config to FT-710 / 38400 / port 4532', () => {
    const f = formFromConfig(undefined);
    expect(f.rig_model).toBe(1049);
    expect(f.rig_baud).toBe(38400);
    expect(f.rigctld_port).toBe(4532);
    expect(f.audio_device).toBeNull();
  });
  it('preserves the saved config', () => {
    const f = formFromConfig({ rig_model: 3073, audio_device: 'USB Audio' });
    expect(f.rig_model).toBe(3073);
    expect(f.audio_device).toBe('USB Audio');
  });
  it('detects custom models', () => {
    expect(isCustomModel(formFromConfig({ rig_model: 9999 }))).toBe(true);
    expect(isCustomModel(formFromConfig({ rig_model: 1049 }))).toBe(false);
  });
  it('labels sources with a default', () => {
    expect(sourceLabel({ rig_model: 'file' }, 'rig_model')).toBe('file');
    expect(sourceLabel(undefined, 'audio_device')).toBe('default');
  });
  it('exposes common baud rates including the FT-710 value', () => {
    expect(BAUD_OPTIONS).toContain(38400);
    expect(BAUD_OPTIONS).toContain(115200);
  });
});
```

- [ ] **步骤 5：运行 vitest**

运行：`cd desktop/ft8web && npx vitest run`
预期：PASS（含既有 mrrcClient/streams/pskReporter 测试）

- [ ] **步骤 6：App.tsx 接线**

`App.tsx`：
1. 导入：`import { DeviceSettings, type DeviceForm } from './components/DeviceSettings';`
2. UI state 区加：

```tsx
  // Device configuration (spec 2026-08-10) — station hardware, server-managed.
  const [deviceData, setDeviceData] = useState<Record<string, any> | null>(null);
  const [deviceSaving, setDeviceSaving] = useState(false);
  const [deviceBusy, setDeviceBusy] = useState(false);
```

3. `openSettingsModal` 里 `mrrc.settings()` 之后加（并行拉设备数据）：

```tsx
    const devicesRes = await mrrc.devices();
    if (devicesRes.ok) setDeviceData(devicesRes.body);
```

4. 回调（`saveSettings` 之后）：

```tsx
  const saveDeviceConfig = useCallback(async (form: DeviceForm): Promise<string | null> => {
    setDeviceSaving(true);
    try {
      const res = await mrrc.saveDevices(form);
      if (!res.ok) {
        return res.reason === 'tx_active'
          ? 'Devices locked during TX'
          : `Save rejected: ${res.reason ?? res.status}`;
      }
      const refreshed = await mrrc.devices();
      if (refreshed.ok) setDeviceData(refreshed.body);
      return null;
    } finally {
      setDeviceSaving(false);
    }
  }, []);

  const applyDeviceConfig = useCallback(async (): Promise<string | null> => {
    setDeviceBusy(true);
    try {
      const res = await mrrc.applyDevices();
      if (!res.ok) {
        return res.reason === 'tx_active'
          ? 'Devices locked during TX'
          : `Apply rejected: ${res.reason ?? res.status}`;
      }
      return null;
    } finally {
      setDeviceBusy(false);
    }
  }, []);
```

5. 设置弹窗 JSX 里、`Station identity` 块之前插入：

```tsx
              {deviceData && (
                <DeviceSettings
                  config={deviceData.config}
                  source={deviceData.source}
                  audioDevices={deviceData.audio_devices ?? []}
                  serialDevices={deviceData.serial_devices ?? []}
                  busy={deviceSaving || deviceBusy}
                  onSave={saveDeviceConfig}
                  onApply={applyDeviceConfig}
                />
              )}
```

- [ ] **步骤 7：类型检查 + 全量桌面测试**

运行：`cd desktop/ft8web && npx tsc --noEmit && npx vitest run`
预期：无类型错误，全部 PASS

- [ ] **步骤 8：Commit**

```bash
git add desktop/ft8web/src/services/mrrcClient.ts desktop/ft8web/src/services/mrrcClient.test.ts desktop/ft8web/src/components/DeviceSettings.tsx desktop/ft8web/src/components/deviceConfig.test.ts desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): device settings section (hamlib rig + audio, save/apply-restart)"
```

---

### Task 6：手机 PWA（api.js + Devices 标签页 + renderDevices）

**文件：**
- 修改：`server/web/static/index.html`
- 修改：`server/web/static/js/api.js`
- 修改：`server/web/static/js/settings.js`
- 修改：`tests/web/test_static.py`

- [ ] **步骤 1：index.html 加标签页**

`drawer-tabs` 内 `FT8` 之后加：

```html
        <button class="tab" data-tab="devices">Devices</button>
```

- [ ] **步骤 2：api.js 加方法**

`putSetting` 之后加：

```js
  devices: () => request("/devices"),
  saveDevices: (cfg) =>
    request("/devices", { method: "PUT", idempotencyKey: key(), body: cfg }),
  applyDevices: () =>
    request("/devices/apply", { method: "POST", idempotencyKey: key() }),
```

- [ ] **步骤 3：settings.js 的 renderTab 加分支**

`renderTab` 的 `else if (tab === "station") renderStation();` 之后加：

```js
    else if (tab === "devices") renderDevices();
```

并在 `renderStation` 之后新增 `renderDevices`（含事件绑定，仿 renderRadio 的 `content.querySelector` 模式）：

```js
  // Devices: station hardware (hamlib rig + audio, spec 2026-08-10).
  // Save persists server-side without restart; Apply & Restart relaunches
  // rigctld + the server (~20 s disconnect).
  async function renderDevices() {
    content.innerHTML = "<p class='drawer-hint'>Loading device settings…</p>";
    const res = await api.devices();
    if (!res.ok) {
      content.innerHTML = "<p class='drawer-hint'>Device settings unavailable.</p>";
      return;
    }
    const { config = {}, source = {}, audio_devices = [], serial_devices = [],
            curated_rig_models = [], baud_rates = [] } = res;
    const form = {
      rig_model: Number(config.rig_model ?? 1049),
      rig_device: String(config.rig_device ?? ""),
      rig_baud: Number(config.rig_baud ?? 38400),
      rigctld_port: Number(config.rigctld_port ?? 4532),
      audio_device: config.audio_device ?? null,
    };
    const customModel = !curated_rig_models.some((m) => m.model === form.rig_model);
    const modelOptions = curated_rig_models
      .map((m) => `<option value="${m.model}" ${m.model === form.rig_model ? "selected" : ""}>${m.name} (${m.model})</option>`)
      .join("") + `<option value="custom" ${customModel ? "selected" : ""}>Custom…</option>`;
    const serialCustom = form.rig_device && !serial_devices.includes(form.rig_device);
    const serialOptions = serial_devices
      .map((d) => `<option value="${d}" ${d === form.rig_device ? "selected" : ""}>${d}</option>`)
      .join("");
    const audioOptions =
      `<option value="">System default</option>` +
      audio_devices
        .map((d) => `<option value="${d.name}" ${d.name === form.audio_device ? "selected" : ""}>${d.name}</option>`)
        .join("");
    const sourceLine = (key) => `${key}: ${source[key] || "default"}`;
    content.innerHTML = `
      <h3>Devices</h3>
      <label class="setting-row">
        <span>Rig model (hamlib)</span>
        <select data-device-model>${modelOptions}</select>
      </label>
      <div class="device-custom" ${customModel ? "" : "hidden"}>
        <label class="setting-row">
          <span>Custom model number</span>
          <input data-device-model-custom type="number" min="1" value="${form.rig_model}">
        </label>
      </div>
      <label class="setting-row">
        <span>CAT serial device</span>
        <select data-device-serial>
          <option value="">—</option>${serialOptions}
          <option value="custom" ${serialCustom ? "selected" : ""}>Custom…</option>
        </select>
      </label>
      <div class="device-custom-serial" ${serialCustom ? "" : "hidden"}>
        <label class="setting-row">
          <span>Custom serial path</span>
          <input data-device-serial-custom type="text" value="${form.rig_device}">
        </label>
      </div>
      <label class="setting-row">
        <span>Baud rate</span>
        <select data-device-baud>
          ${baud_rates.map((b) => `<option value="${b}" ${b === form.rig_baud ? "selected" : ""}>${b}</option>`).join("")}
        </select>
      </label>
      <label class="setting-row">
        <span>rigctld port</span>
        <input data-device-port type="number" min="1024" max="65535" value="${form.rigctld_port}">
      </label>
      <label class="setting-row">
        <span>Audio device</span>
        <select data-device-audio>${audioOptions}</select>
      </label>
      <p class="drawer-hint dim">Source — ${sourceLine("rig_model")} · ${sourceLine("rig_device")} · ${sourceLine("rig_baud")} · ${sourceLine("rigctld_port")} · ${sourceLine("audio_device")}</p>
      <div class="device-actions" style="display:flex;gap:8px;margin-top:8px">
        <button data-device-save class="cmd">Save</button>
        <button data-device-apply class="cmd">Apply &amp; Restart</button>
      </div>
      <p class="drawer-hint dim">Apply &amp; Restart relaunches rigctld and the
        server — about 20 seconds of disconnect, then log in again.</p>`;

    const customWrap = content.querySelector(".device-custom");
    const serialCustomWrap = content.querySelector(".device-custom-serial");
    content.querySelector("[data-device-model]")?.addEventListener("change", (e) => {
      const custom = e.target.value === "custom";
      if (customWrap) customWrap.hidden = !custom;
    });
    content.querySelector("[data-device-serial]")?.addEventListener("change", (e) => {
      const custom = e.target.value === "custom";
      if (serialCustomWrap) serialCustomWrap.hidden = !custom;
    });
    const readForm = () => ({
      rig_model: Number((content.querySelector("[data-device-model]")?.value === "custom"
        ? content.querySelector("[data-device-model-custom]")?.value
        : content.querySelector("[data-device-model]")?.value) || 1049),
      rig_device: (() => {
        const sel = content.querySelector("[data-device-serial]")?.value;
        if (sel === "custom") return String(content.querySelector("[data-device-serial-custom]")?.value ?? "");
        return String(sel ?? "");
      })(),
      rig_baud: Number(content.querySelector("[data-device-baud]")?.value || 38400),
      rigctld_port: Number(content.querySelector("[data-device-port]")?.value || 4532),
      audio_device: content.querySelector("[data-device-audio]")?.value || null,
    });
    content.querySelector("[data-device-save]")?.addEventListener("click", async () => {
      const result = await api.saveDevices(readForm());
      if (!result.ok) {
        showToast(result.reason === "tx_active" ? "Devices locked during TX" : `Save: ${result.reason || result.status}`);
      } else {
        showToast("Device settings saved (apply to restart)");
        renderDevices();  // refresh source labels
      }
    });
    content.querySelector("[data-device-apply]")?.addEventListener("click", async () => {
      if (!window.confirm("Apply will restart rigctld and the server.\n~20 s disconnect, then log in again. Continue?")) return;
      const result = await api.applyDevices();
      if (!result.ok) {
        showToast(result.reason === "tx_active" ? "Devices locked during TX" : `Apply: ${result.reason || result.status}`);
      } else {
        showToast("Restarting… reconnecting shortly");
      }
    });
  }
```

（注：`apply` 会杀掉当前 server；若在 apply 后仍显示 toast，会在断连时消失——无碍。PWA 的 401 处理已在 api.js 里 `location.reload()`，重启后自动回登录页。）

- [ ] **步骤 4：静态测试断言标签存在**

`tests/web/test_static.py` 追加：

```python
def test_index_has_devices_drawer_tab(client: TestClient) -> None:
    response = client.get("/static/index.html")
    assert response.status_code == 200
    assert b'data-tab="devices"' in response.content
```

（按该文件现有 fixture 名/导入调整；若文件无 TestClient fixture，用 `server.web.api.create_app` 直接 `TestClient(...).get("/static/index.html")`。）

- [ ] **步骤 5：运行测试**

运行：`venv/bin/python -m pytest tests/web/test_static.py tests/web/test_devices.py -v`
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add server/web/static/index.html server/web/static/js/api.js server/web/static/js/settings.js tests/web/test_static.py
git commit -m "feat(pwa): devices drawer tab (hamlib rig + audio, save/apply-restart)"
```

---

### Task 7：文档同步 + 全量回归

**文件：**
- 修改：`AGENTS.md`（模块表：`server/web/api.py` 行注记 devices 端点；`restart.sh` 行注记 JSON 读取；`server/engine/` 行可注记 device_config）
- 修改：`tests/README.md`（覆盖清单加 device_config/devices 端点）
- 修改：`SDD/14-version-history.md`（新条目）
- 修改：`SDD/README`（Quick Facts 版本 bump，若存在）
- 修改：`SDD/12-…`（配置章节，§12.6：新增 device-config.json 与优先级）
- 修改：`SDD/08-architecture-decisions.md`（AD-008 注记：rigctld 参数改配置文件驱动，UI 可改；仍为串口唯一 owner）
- 检查：`docs/superpowers/plans/2026-08-10-device-config.md`（本计划）与规格对齐

- [ ] **步骤 1：全量回归**

运行：`cd /Users/cheenle/HAM/ft8 && venv/bin/python -m pytest tests/ -q && cd desktop/ft8web && npx tsc --noEmit && npx vitest run`
预期：全部 PASS，无类型错误

- [ ] **步骤 2：SDD 校验**

运行：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged`
预期：`clean`（或可解释的警告）

- [ ] **步骤 3：文档编辑（按上文清单逐文件）**

- **AGENTS.md** 模块表 `server/web/api.py` 行末追加：`；`/api/v1/devices` 设备配置（HAMLIB rig + 音频，保存/应用重启）`
- **AGENTS.md** `restart.sh` 行追加：`；启动前读 `data/device-config.json`（存在时覆盖 RIG_* 参数）`
- **SDD/08** AD-008 追加 consequence 注记（一句话）
- **SDD/12** 配置章节追加 device-config.json 小节（字段表 + 优先级：file > env > default）
- **SDD/14** 追加条目（功能、文件、回归、测试环境：本机 macOS + FT-710/rigctld 配置场景）
- **tests/README.md** 覆盖清单加 `tests/engine/test_device_config.py` 与 `tests/web/test_devices.py`

- [ ] **步骤 4：最终 Commit**

```bash
git add AGENTS.md tests/README.md SDD/ README.md 2>/dev/null
git commit -m "docs: device config — SDD/AD-008 note, config chapter, version history, module table"
```

---

## 验收标准（全部完成后核对）

- [ ] `data/device-config.json` 生成/读取/原子写正确，缺省回退 env（`pytest tests/engine/test_device_config.py` 绿）
- [ ] `GET/PUT/POST /devices` 全链路（`tests/web/test_devices.py` 绿），TX 时 409
- [ ] `restart.sh` 语法校验通过，`bash -n` 无错，JSON 覆盖 RIG_* 生效（人工冒烟）
- [ ] 桌面 Settings 弹窗出现 Devices 分区，保存/应用重启可用，vitest + tsc 绿
- [ ] PWA ☰ 抽屉出现 Devices 标签页，保存/应用可用（人工 + test_static）
- [ ] `sdd check --staged` clean
- [ ] 手动端到端（选做，需电台）：改音频设备 → 保存 → 应用 → server 自动重启 → 重新登录 → 新音频设备生效、CAT 正常
