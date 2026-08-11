"""sync: one full round — pull, merge, push, confirm, requeue (spec §3.2)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from rumlog_sync.config import RumlogConfig
from rumlog_sync.ft8_db import Ft8Db
from rumlog_sync.sync import run_sync_once


@dataclass
class FakePusher:
    """Records every push batch; returns the batch size unless ``ok`` is off."""

    calls: list[list[dict[str, object]]] = field(default_factory=list)
    ok: bool = True

    def __call__(self, records: list[dict[str, object]]) -> int:
        self.calls.append(records)
        return len(records) if self.ok else 0


def _cfg(ft8_path: Path, rumlog_path: Path, retries: int = 3) -> RumlogConfig:
    return {
        "ft8_db": str(ft8_path),
        "rumlog_db": str(rumlog_path),
        "my_call": "BG1SB",
        "my_grid": "ON80DA",
        "udp_host": "127.0.0.1",
        "udp_port": 2237,
        "udp_id": "MRRC-FT8-SYNC",
        "confirm_retries": retries,
        "lock_path": str(ft8_path.parent / "sync.lock"),
    }


def _rumlog_row(callsign: str, pk: int, unix_epoch: float, band: str = "20m"):
    return {
        "ZCALLSIGN": callsign,
        "Z_PK": pk,
        "unix_epoch": unix_epoch,
        "ZQRG": 14.074684,
        "ZBAND": band,
        "ZMODE": "FT8",
        "ZLOCATOR": "PM02",
        "ZRSTTX": "-12",
        "ZRSTRX": "-12",
        "ZUUID": bytes.fromhex(f"{pk:032x}"),
    }


def test_full_merge_then_incremental(
    ft8_db_path, tmp_path: Path, make_rumlog_db
) -> None:
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(
        rumlog,
        [
            _rumlog_row("TL8GD", 27139, 1_795_384_049.0),
            _rumlog_row("BI4QMU", 27140, 1_795_384_100.0, band="40m"),
        ],
    )
    pusher = FakePusher()
    report = run_sync_once(_cfg(ft8_db_path, rumlog), pusher=pusher)
    assert report.pulled == 2
    assert report.inserted == 2
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    # Both pulled records are now present with rumlog_uuid backfilled.
    rows = db._con.execute(
        "SELECT dx_call, rumlog_uuid, pushed_to_rumlog FROM qso ORDER BY id"
    ).fetchall()
    assert [(r["dx_call"], r["rumlog_uuid"]) for r in rows] == [
        ("TL8GD", f"{27139:032x}".upper()),
        ("BI4QMU", f"{27140:032x}".upper()),
    ]
    # Pulled records already exist in RUMLogNG → never queued for push.
    assert all(r["pushed_to_rumlog"] == 1 for r in rows)
    # Nothing pushed: pulled records are already on the RUMLogNG side.
    assert pusher.calls == []
    # Second round is a no-op (cursor advanced).
    report2 = run_sync_once(_cfg(ft8_db_path, rumlog), pusher=pusher)
    assert report2.pulled == 0
    assert report2.inserted == 0


def test_conflict_resolution_prefers_rumlog(
    ft8_db_path, tmp_path: Path, make_rumlog_db
) -> None:
    # Pre-existing FT8 record (from jtdx import) with different grid/RST.
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.insert_record(
        {
            "my_call": "BG1SB", "my_grid": "ON80DA", "dx_call": "TL8GD",
            "dx_grid": "", "report_sent": -10, "report_rcvd": -10,
            "started_utc": "232300", "mode": "FT8", "freq_hz": 14_074_684,
            "band": "20m", "status": "completed",
            "completed_epoch": 1_795_384_049.0, "rumlog_uuid": "", "source": "jtdx",
        }
    )
    db.commit()
    db.close()  # release the write lock before the round runs
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(
        rumlog, [_rumlog_row("TL8GD", 27139, 1_795_384_049.0)]
    )
    report = run_sync_once(
        _cfg(ft8_db_path, rumlog), pusher=FakePusher()
    )
    assert report.inserted == 0
    assert report.updated == 1
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    row = db.get_record(1)
    assert row is not None
    assert row["dx_grid"] == "PM02"
    assert row["report_sent"] == -12
    assert row["rumlog_uuid"] == f"{27139:032x}".upper()
    # Matched record exists in RUMLogNG → confirmed, not queued for push.
    assert row["pushed_to_rumlog"] == 1
    db.close()


def test_push_new_ft8_record_and_confirm_next_round(
    ft8_db_path, tmp_path: Path, make_rumlog_db
) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.insert_record(
        {
            "my_call": "BG1SB", "my_grid": "ON80DA", "dx_call": "V85T",
            "dx_grid": "", "report_sent": None, "report_rcvd": None,
            "started_utc": "162730", "mode": "FT8", "freq_hz": 14_076_000,
            "band": "20m", "status": "completed",
            "completed_epoch": 1_753_351_889.0, "rumlog_uuid": "", "source": "live",
        }
    )
    db.commit()
    db.close()  # release the write lock before round 1 runs
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(rumlog, [])  # RUMLogNG empty for round 1
    pusher = FakePusher()
    report = run_sync_once(_cfg(ft8_db_path, rumlog), pusher=pusher)
    assert report.pushed == 1
    assert len(pusher.calls) == 1
    pushed_rec = pusher.calls[0][0]
    assert pushed_rec["dx_call"] == "V85T"
    assert pushed_rec["completed_epoch"] == 1_753_351_889.0
    assert pushed_rec["band"] == "20m"
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    row = db.get_record(1)
    assert row is not None
    assert row["pushed_to_rumlog"] == 1
    db.close()
    # Round 2: RUMLogNG now contains the QSO → uuid backfilled, confirmed.
    make_rumlog_db(
        rumlog,
        [
            {
                "ZCALLSIGN": "V85T", "Z_PK": 1,
                "unix_epoch": 1_753_351_889.0, "ZQRG": 14.076,
                "ZBAND": "20m", "ZMODE": "FT8", "ZLOCATOR": "",
                "ZRSTTX": None, "ZRSTRX": None,
                "ZUUID": bytes.fromhex("11111111111111111111111111111111"),
            }
        ],
    )
    report2 = run_sync_once(_cfg(ft8_db_path, rumlog), pusher=pusher)
    assert report2.updated == 1  # conflict-resolved (same record) → uuid backfill
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    row2 = db.get_record(1)
    assert row2 is not None
    assert row2["rumlog_uuid"] == "11111111111111111111111111111111"
    # Confirmed: the unconfirmed-push counter for qso 1 was cleared.
    assert json.loads(db.state_get("push_pending") or "{}") == {}
    db.close()


def test_unconfirmed_push_requeued_after_retries(
    ft8_db_path, tmp_path: Path, make_rumlog_db
) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.insert_record(
        {
            "my_call": "BG1SB", "my_grid": "ON80DA", "dx_call": "AA5AT",
            "dx_grid": "", "report_sent": None, "report_rcvd": None,
            "started_utc": "235833", "mode": "FT8", "freq_hz": 24_916_000,
            "band": "12m", "status": "completed",
            "completed_epoch": 1_727_952_233.0, "rumlog_uuid": "", "source": "live",
        }
    )
    db.commit()
    db.close()  # release the write lock before the retry loop
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(rumlog, [])
    cfg = _cfg(ft8_db_path, rumlog, retries=2)
    for _ in range(3):
        run_sync_once(cfg, pusher=FakePusher())
    # retries=2: round 2 ages the counter, round 3 requeues and re-pushes.
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    row = db.get_record(1)
    assert row is not None
    assert row["pushed_to_rumlog"] == 1  # requeued then re-pushed in round 3
    pending = json.loads(db.state_get("push_pending") or "{}")
    assert int(pending["1"]) == 1
    db.close()


def test_push_failure_keeps_records_queued(
    ft8_db_path, tmp_path: Path, make_rumlog_db
) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.insert_record(
        {
            "my_call": "BG1SB", "my_grid": "ON80DA", "dx_call": "K1BZ",
            "dx_grid": "", "report_sent": None, "report_rcvd": None,
            "started_utc": "235930", "mode": "FT8", "freq_hz": 21_074_000,
            "band": "12m", "status": "completed",
            "completed_epoch": 1_728_063_570.0, "rumlog_uuid": "", "source": "live",
        }
    )
    db.commit()
    db.close()
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(rumlog, [])
    pusher = FakePusher(ok=False)  # osascript unavailable
    report = run_sync_once(_cfg(ft8_db_path, rumlog), pusher=pusher)
    assert report.pushed == 0
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    row = db.get_record(1)
    assert row is not None
    assert row["pushed_to_rumlog"] == 0  # stays queued for the next round
    db.close()


def test_soft_error_when_rumlog_db_missing(ft8_db_path, tmp_path: Path) -> None:
    report = run_sync_once(
        _cfg(ft8_db_path, tmp_path / "missing.sqlite"), pusher=FakePusher()
    )
    assert report.pulled == 0
    assert report.errors  # recorded, not raised
