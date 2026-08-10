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

# _ENV_KEY: env names restart.sh evals when launching rigctld (launch vars).
# _SOURCE_ENV: env names the server actually reads when connecting — the
# source detection in ``source_of`` must report against these, not the
# restart.sh vars. rigctld_port is the one divergence: the server connects
# via ``MRRC_FT8_RIGCTLD`` (host:port) while restart.sh is launched with
# ``MRRC_FT8_RIGCTLD_PORT`` (port only).
_ENV_KEY = {
    "rig_model": "MRRC_FT8_RIG_MODEL",
    "rig_device": "MRRC_FT8_RIG_DEVICE",
    "rig_baud": "MRRC_FT8_RIG_BAUD",
    "rigctld_port": "MRRC_FT8_RIGCTLD_PORT",
    "audio_device": "MRRC_FT8_AUDIO_DEVICE",
}

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

    payload = {k: cfg[k] for k in _CONFIG_KEYS if k in cfg and cfg[k] not in (None, "")}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="device-config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        log.error("device config write failed: %s", exc)
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
        # 空/空白串口 = "不改动"（本站串口可能只在 restart.sh 默认值里，UI 表单
        # 总是提交该字段）；非空才要求绝对 /dev/ 路径。
        if device and device.strip():
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
            log.error("restart script missing: %s", self.script)
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
