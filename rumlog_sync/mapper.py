"""Pure mappings between RUMLogNG Core Data rows and FT8 qso records.

Spec §4/§5: Core Data timestamps count seconds since 2001-01-01 UTC
(offset 978307200 s).  Cross-db dedupe matches dx_call+band with a 120 s
completion-time window.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

CORE_DATA_EPOCH_OFFSET = 978_307_200
MATCH_WINDOW_S = 120.0
_DEFAULT_MODE = "FT8"


def to_unix(core_data_seconds: float) -> float:
    """Core Data timestamp (seconds since 2001-01-01) → Unix epoch."""

    return core_data_seconds + CORE_DATA_EPOCH_OFFSET


def _to_float(value: Any, default: float = 0.0) -> float:
    """Coerce a DB value to float; None/garbage → default."""

    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any) -> int | None:
    """Coerce a DB value to int; None/garbage → None."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def derive_started_utc(epoch: float) -> str:
    """Unix epoch → ``HHMMSS`` display string (spec §4)."""

    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H%M%S")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_report(value: object) -> int | None:
    """``'+00'`` / ``'-15'`` / ``'59'`` → int; empty/garbage → None."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _safe_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def map_rumlog_row(row: dict[str, object], *, my_call: str, my_grid: str) -> dict[str, object]:
    """One ZCORE_QSO row → FT8 qso record dict (spec §4 table)."""

    epoch = to_unix(_to_float(row.get("ZDATETIME")))
    freq_hz = round(_to_float(row.get("ZQRG")) * 1_000_000)
    uuid_raw = row.get("ZUUID")
    uuid_hex = uuid_raw.hex().upper() if isinstance(uuid_raw, bytes) else ""
    return {
        "my_call": my_call,
        "my_grid": my_grid,
        "dx_call": _safe_str(row.get("ZCALLSIGN")),
        "dx_grid": _safe_str(row.get("ZLOCATOR")),
        "report_sent": parse_report(row.get("ZRSTTX")),
        "report_rcvd": parse_report(row.get("ZRSTRX")),
        "started_utc": derive_started_utc(epoch),
        "mode": _safe_str(row.get("ZMODE")) or _DEFAULT_MODE,
        "freq_hz": freq_hz,
        "band": _safe_str(row.get("ZBAND")),
        "status": "completed",
        "completed_epoch": epoch,
        "rumlog_uuid": uuid_hex,
        "source": "rumlog",
    }


def _fmt_report(value: object) -> str:
    return "" if value is None else f"{_to_int(value):+03d}"


def build_push_adif_fields(rec: dict[str, object]) -> dict[str, str]:
    """FT8 qso record → ADIF fields for the WSJT-X QSO_LOGGED payload."""

    epoch = _to_float(rec["completed_epoch"])
    date_utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
    freq_mhz = _to_float(rec.get("freq_hz")) / 1_000_000.0
    return {
        "CALL": _safe_str(rec.get("dx_call")),
        "GRIDSQUARE": _safe_str(rec.get("dx_grid")),
        "MODE": _safe_str(rec.get("mode")) or _DEFAULT_MODE,
        "BAND": _safe_str(rec.get("band")),
        "FREQ": f"{freq_mhz:.6f}",
        "RST_SENT": _fmt_report(rec.get("report_sent")),
        "RST_RCVD": _fmt_report(rec.get("report_rcvd")),
        "QSO_DATE": date_utc.strftime("%Y%m%d"),
        "TIME_ON": _safe_str(rec.get("started_utc")) or date_utc.strftime("%H%M%S"),
        "TIME_OFF": date_utc.strftime("%H%M%S"),
        "STATION_CALLSIGN": _safe_str(rec.get("my_call")),
    }


def same_qso_match(dx_call: str, band: str, epoch_a: float, epoch_b: float) -> bool:
    """True when both sides describe the same QSO (spec §5)."""

    return dx_call != "" and band != "" and abs(epoch_a - epoch_b) <= MATCH_WINDOW_S
