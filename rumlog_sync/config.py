"""Configuration for the RUMLogNG bidirectional sync program."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast

log = logging.getLogger(__name__)

Validator = Callable[[object], bool] | type


class RumlogConfig(TypedDict):
    """Validated sync configuration (spec §3.1)."""

    ft8_db: str
    rumlog_db: str
    my_call: str
    my_grid: str
    udp_host: str
    udp_port: int
    udp_id: str
    confirm_retries: int
    lock_path: str


DEFAULT_CONFIG: RumlogConfig = {
    "ft8_db": "data/mrrc-ft8.db",
    "rumlog_db": (
        "/Users/cheenle/Library/Containers/de.dl2rum.RUMlogNG/Data/Library/"
        "Application Support/RUMLogNG/CoreQsoModel_1.sqlite"
    ),
    "my_call": "BG1SB",
    "my_grid": "ON80DA",
    "udp_host": "127.0.0.1",
    "udp_port": 2237,
    "udp_id": "MRRC-FT8-SYNC",
    "confirm_retries": 3,
    "lock_path": "data/rumlog-sync.lock",
}

_VALIDATORS: dict[str, Validator] = {
    "ft8_db": str,
    "rumlog_db": str,
    "my_call": str,
    "my_grid": str,
    "udp_host": str,
    "udp_port": lambda v: isinstance(v, int) and 1 <= v <= 65535,
    "udp_id": str,
    "confirm_retries": lambda v: isinstance(v, int) and v >= 1,
    "lock_path": str,
}


def validate_config(cfg: dict[str, object]) -> RumlogConfig:
    """Reject unknown keys and out-of-range values; return a safe copy."""

    unknown = set(cfg) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    merged = {**DEFAULT_CONFIG, **cfg}
    for key, check in _VALIDATORS.items():
        value = merged[key]
        if isinstance(check, type):
            if not isinstance(value, check):
                raise ValueError(f"config {key}: expected {check.__name__}, got {value!r}")
        elif not check(value):
            raise ValueError(f"config {key}: invalid value {value!r}")
    return cast(RumlogConfig, merged)


def load_config(path: str | Path) -> RumlogConfig:
    """Load JSON config over defaults; missing file is not an error."""

    p = Path(path)
    if not p.exists():
        return cast(RumlogConfig, dict(DEFAULT_CONFIG))
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"config {p}: expected JSON object")
    return validate_config(raw)
