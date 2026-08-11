"""One sync round: pull RUMLogNG → merge into FT8 db; push FT8 → RUMLogNG.

Spec §3.2.  Pull: read-only cursor past ``last_seen_pk``, map each row,
dedupe against the FT8 db (UUID first, then dx_call+band with a 120 s
window), insert new or update existing with RUMLogNG winning.  Push:
records with ``pushed_to_rumlog=0`` go out as WSJT-X UDP HEARTBEAT +
QSO_LOGGED; unconfirmed pushes are requeued after ``confirm_retries``
rounds.  Every step is transactional per record; failures are logged and
skip that record, never aborting the round mid-way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .apple_push import push_via_applescript
from .config import RumlogConfig
from .ft8_db import Ft8Db
from .mapper import build_push_adif_fields, map_rumlog_row
from .rumlog_reader import RUMlogReader

log = logging.getLogger(__name__)

Pusher = Callable[[list[dict[str, object]]], int]


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
                ft8.update_from_rumlog(existing, merged)
                # The QSO exists in RUMLogNG → never push it back; confirm.
                ft8.confirm_pushed(existing)
                report.updated += 1
                log.info("updated QSO %s from RUMLogNG", rec["dx_call"])
            report.pulled += 1
            last_pk = max(last_pk, pk)
        ft8.state_set("last_seen_pk", str(last_pk))

        # ---- Step 2: push --------------------------------------------------
        report.requeued = ft8.tick_unconfirmed(cfg["confirm_retries"])
        pending = ft8.pending_push_records()
        if pending:
            pushed = pusher(pending)
            if pushed > 0:
                for rec in pending[:pushed]:
                    ft8.mark_pushed(int(rec["id"]))
                report.pushed = pushed
            else:
                log.warning(
                    "push failed; %d record(s) stay queued", len(pending)
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
