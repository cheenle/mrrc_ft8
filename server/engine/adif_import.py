"""JTDX wsjtx_log.adi → canonical store incremental import (AD-014 store).

WSJT-X/JTDX append every completed QSO to the ADIF export live, so the
parser must tolerate a half-written trailing line (no ``<eor>`` yet) and
malformed records.  Dedupe key ``(dx_call, utc date, started_utc, band)``
collapses JTDX's same-second duplicate attempts and keeps re-syncs
idempotent; cross-source by default so a live QSO is never re-imported.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .repository import Repository
from .sequencer import QSORecord

log = logging.getLogger(__name__)

_FIELD_RE = re.compile(r"<([a-z_]+):\d+>([^<]*)")
_EOH = "<eoh>"
_EOR = "<eor>"


def parse_adif(text: str) -> list[dict[str, str]]:
    """Parse an ADIF export into per-record field dicts (tolerant)."""

    records: list[dict[str, str]] = []
    for line in text.splitlines():
        if _EOH in line or _EOR not in line:
            continue  # header line / half-written trailing record
        fields: dict[str, str] = {}
        for tag, value in _FIELD_RE.findall(line):
            fields[tag] = value.strip()
        if fields:
            records.append(fields)
    return records


def _parse_report(value: str | None) -> int | None:
    """``'+00'`` / ``'-15'`` → int; anything else → None."""

    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _utc_epoch(date_yyyymmdd: str, time_hhmmss: str) -> float:
    """``'20230227'`` + ``'005730'`` → UTC epoch; 0.0 on garbage."""

    try:
        dt = datetime.strptime(f"{date_yyyymmdd} {time_hhmmss}", "%Y%m%d %H%M%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def map_record(
    fields: dict[str, str], *, my_call: str, my_grid: str
) -> tuple[QSORecord, float] | None:
    """One ADIF record → ``(QSORecord, completed_epoch)``; None when the
    record lacks a valid call/qso_date/time_on."""

    call = fields.get("call", "").strip()
    qso_date = fields.get("qso_date", "").strip()
    time_on = fields.get("time_on", "").strip()
    if not call or len(qso_date) != 8 or not qso_date.isdigit():
        return None
    if len(time_on) != 6 or not time_on.isdigit():
        return None
    freq_hz = 0
    freq = fields.get("freq", "").strip()
    if freq:
        try:
            freq_hz = round(float(freq) * 1_000_000)
        except ValueError:
            pass
    date_off = fields.get("qso_date_off", qso_date).strip() or qso_date
    time_off = fields.get("time_off", time_on).strip() or time_on
    record = QSORecord(
        my_call=my_call,
        my_grid=my_grid.upper(),
        dx_call=call,
        dx_grid=fields.get("gridsquare", "").strip(),
        report_sent=_parse_report(fields.get("rst_sent")),
        report_rcvd=_parse_report(fields.get("rst_rcvd")),
        started_utc=time_on,
        mode=fields.get("mode", "FT8").strip() or "FT8",
        freq_hz=freq_hz,
        band=fields.get("band", "").strip(),
    )
    return record, _utc_epoch(date_off, time_off)


def dedupe_key(
    record: QSORecord, epoch: float, qso_date: str
) -> tuple[str, str, str, str]:
    """Collision key: same call + UTC date + start second + band = one QSO."""

    if len(qso_date) == 8 and qso_date.isdigit():
        date = qso_date
    elif epoch:
        date = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%d")
    else:
        date = ""
    return (record.dx_call, date, record.started_utc, record.band)
