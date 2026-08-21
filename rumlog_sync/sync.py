"""One sync round: pull RUMLogNG → merge into FT8 db; push FT8 → RUMLogNG.

Spec §3.2.  Pull: read-only cursor past ``last_seen_pk``, map each row,
dedupe against the FT8 db (UUID first, then dx_call+band with a 120 s
window), insert new or update existing with RUMLogNG winning.  Push:
records with ``pushed_to_rumlog=0`` go out via the RUMLogNG AppleScript
API (``apple_push``); unconfirmed pushes are requeued after
``confirm_retries`` rounds.  Every step is transactional per record; failures are logged and
skip that record, never aborting the round mid-way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .apple_push import push_via_applescript
from .config import RumlogConfig
from .ft8_db import Ft8Db
from .mapper import map_rumlog_row
from .rumlog_reader import RUMlogReader

log = logging.getLogger(__name__)

Pusher = Callable[[list[dict[str, object]]], list[dict[str, object]]]


def _qso_id(rec: dict[str, object]) -> int:
    """Coerce a record's sqlite ``id`` value to int (DB rows are ints)."""

    value: Any = rec.get("id")
    if value is None:
        raise ValueError(f"record without id: {rec}")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"record id is not an int: {value!r}") from None


def _rec_epoch(rec: dict[str, object]) -> float:
    """Coerce ``completed_epoch`` to float; None/garbage → 0.0."""

    try:
        return float(rec.get("completed_epoch") or 0)
    except (TypeError, ValueError):
        return 0.0


def _same_push_qso(a: dict[str, object], b: dict[str, object]) -> bool:
    """True when two pending records describe the same QSO (AD-016 §5).

    Mirrors ``Ft8Db.find_all_existing``: same ``dx_call``, an equal-or-empty
    ``band`` (empty is a wildcard on either side), and completion times within
    the 120 s window.
    """

    if str(a.get("dx_call") or "") != str(b.get("dx_call") or ""):
        return False
    band_a = str(a.get("band") or "")
    band_b = str(b.get("band") or "")
    if band_a and band_b and band_a != band_b:
        return False
    return abs(_rec_epoch(a) - _rec_epoch(b)) <= 120.0


def _dedupe_pending(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep one record per QSO before pushing (AD-016 §5).

    The canonical db can hold duplicate historical rows (JTDX re-imports);
    pushing every duplicate would duplicate QSOs in RUMLogNG.  The window is
    a sliding ±120 s around each candidate — the same predicate the pull step
    uses for confirmation — not fixed 120 s buckets, which would leak a
    duplicate when two copies straddle a bucket boundary (e.g. 119 s vs
    121 s) and would treat an empty band as distinct from a filled one.
    Pending batches are small (a handful per round), so the pairwise scan is
    fine; the one-time large first-run merge has already completed.
    """

    kept: list[dict[str, object]] = []
    for rec in records:
        if any(_same_push_qso(rec, k) for k in kept):
            continue
        kept.append(rec)
    if len(kept) < len(records):
        log.info("deduped push queue: %d → %d", len(records), len(kept))
    return kept


@dataclass
class SyncReport:
    pulled: int = 0
    inserted: int = 0
    updated: int = 0
    pushed: int = 0
    requeued: int = 0
    errors: list[str] = field(default_factory=list)


def run_sync_once(
    cfg: RumlogConfig, *, pusher: Pusher = push_via_applescript
) -> SyncReport:
    """Execute one full pull+merge+push round; returns a report."""

    report = SyncReport()
    ft8 = Ft8Db(cfg["ft8_db"])
    ft8.ensure_schema()
    try:
        reader = RUMlogReader(cfg["rumlog_db"])
    except (OSError, RuntimeError) as exc:  # open failure / schema mismatch
        report.errors.append(f"cannot open RUMLogNG db: {exc}")
        ft8.close()
        return report

    my_call = cfg["my_call"]
    my_grid = cfg["my_grid"]
    try:
        # ---- Step 1: pull -------------------------------------------------
        last_pk = int(ft8.state_get("last_seen_pk") or 0)
        for row in reader.fetch_since(last_pk):
            pk = int(row["Z_PK"])
            rec = map_rumlog_row(row, my_call=my_call, my_grid=my_grid)
            uuid_hex = str(rec["rumlog_uuid"])
            existing = ft8.find_by_uuid(uuid_hex) if uuid_hex else None
            if existing is None:
                existing = ft8.find_existing(
                    str(rec["dx_call"]), str(rec["band"]), float(rec["completed_epoch"])
                )
            if existing is None:
                ft8.insert_record(rec)
                report.inserted += 1
                log.info("pulled new QSO %s %s", rec["dx_call"], rec["band"])
            else:
                # RUMLogNG wins on its fields; keep FT8 completion times.
                merged = dict(rec)
                # Confirm EVERY duplicate of this QSO, not just the first
                # match — duplicate historical rows would otherwise requeue
                # forever and duplicate QSOs in RUMLogNG.
                matched = ft8.find_all_existing(
                    str(rec["dx_call"]), str(rec["band"]), float(rec["completed_epoch"])
                )
                for qso_id in matched:
                    ft8.update_from_rumlog(qso_id, merged)
                    ft8.confirm_pushed(qso_id)
                    report.updated += 1
                log.info("updated QSO %s from RUMLogNG (%d row(s))", rec["dx_call"], len(matched))
            report.pulled += 1
            last_pk = max(last_pk, pk)
        ft8.state_set("last_seen_pk", str(last_pk))

        # ---- Step 2: push --------------------------------------------------
        report.requeued = ft8.tick_unconfirmed(cfg["confirm_retries"])
        pending = _dedupe_pending(ft8.pending_push_records())
        if pending:
            pushed_records = pusher(pending)
            for rec in pushed_records:
                ft8.mark_pushed(_qso_id(rec))
            report.pushed = len(pushed_records)
            if len(pushed_records) < len(pending):
                log.warning(
                    "push partial: %d/%d record(s) stay queued",
                    len(pending) - len(pushed_records),
                    len(pending),
                )

        ft8.record_audit(
            "rumlog_sync",
            f"pulled={report.pulled} inserted={report.inserted}"
            f" updated={report.updated} pushed={report.pushed}"
            f" requeued={report.requeued}",
        )
        ft8.commit()
    except Exception as exc:  # noqa: BLE001 — a round must never kill the crontab job
        report.errors.append(f"round failed: {exc!r}")
        log.exception("rumlog_sync round failed")
        ft8.rollback()
    finally:
        reader.close()
        ft8.close()
    return report
