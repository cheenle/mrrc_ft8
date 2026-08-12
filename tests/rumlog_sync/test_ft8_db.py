"""ft8_db: idempotent migration, dedupe lookup, upsert, push state machine."""

from __future__ import annotations

from rumlog_sync.ft8_db import Ft8Db


def _record(
    dx_call: str = "TL8GD",
    epoch: float = 1_795_384_049.0,
    band: str = "20m",
    rumlog_uuid: str = "",
    source: str = "rumlog",
    dx_grid: str = "PM02",
):
    return {
        "my_call": "BG1SB",
        "my_grid": "ON80DA",
        "dx_call": dx_call,
        "dx_grid": dx_grid,
        "report_sent": -12,
        "report_rcvd": -12,
        "started_utc": "232300",
        "mode": "FT8",
        "freq_hz": 14_074_684,
        "band": band,
        "status": "completed",
        "completed_epoch": epoch,
        "rumlog_uuid": rumlog_uuid,
        "source": source,
    }


def test_migration_is_idempotent_and_adds_columns(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.ensure_schema()  # second run must not raise
    cols = {
        r["name"]
        for r in db._con.execute("PRAGMA table_info(qso)").fetchall()
    }
    assert {"rumlog_uuid", "pushed_to_rumlog"} <= cols


def test_insert_and_find_by_window(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    qso_id = db.insert_record(_record())
    assert db.find_existing("TL8GD", "20m", 1_795_384_100.0) == qso_id  # +51 s
    assert db.find_existing("TL8GD", "20m", 1_795_384_300.0) is None  # +251 s
    assert db.find_existing("TL8GD", "40m", 1_795_384_100.0) is None  # band


def test_find_existing_tolerates_empty_band(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    # Early live rows were logged without a band; RUMLogNG fills one in.
    qso_id = db.insert_record(_record(band=""))
    assert db.find_existing("TL8GD", "40m", 1_795_384_100.0) == qso_id
    assert db.find_existing("TL8GD", "", 1_795_384_100.0) == qso_id
    assert db.find_existing("TL8GD", "15m", 1_795_384_300.0) is None  # outside window
    assert db.find_all_existing("TL8GD", "40m", 1_795_384_100.0) == [qso_id]


def test_find_by_uuid_precise(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    qso_id = db.insert_record(_record(rumlog_uuid="C9DE3F473B90413DA8F6049CDFD26A3B"))
    assert db.find_by_uuid("C9DE3F473B90413DA8F6049CDFD26A3B") == qso_id
    assert db.find_by_uuid("00000000000000000000000000000000") is None


def test_update_from_rumlog_overwrites_fields(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    qso_id = db.insert_record(_record(dx_grid=""))
    db.update_from_rumlog(
        qso_id,
        {
            "dx_grid": "PM02",
            "report_sent": -10,
            "report_rcvd": -11,
            "band": "20m",
            "mode": "FT8",
            "freq_hz": 14_074_684,
            "rumlog_uuid": "C9DE3F473B90413DA8F6049CDFD26A3B",
        },
    )
    row = db.get_record(qso_id)
    assert row["dx_grid"] == "PM02"
    assert row["report_sent"] == -10
    assert row["rumlog_uuid"] == "C9DE3F473B90413DA8F6049CDFD26A3B"
    # Completion time fields are NOT overwritten (spec §4).
    assert row["completed_epoch"] == 1_795_384_049.0
    assert row["started_utc"] == "232300"


def test_push_state_machine(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    qso_id = db.insert_record(_record(source="live"))  # needs a push
    assert db.pending_push() == [qso_id]
    db.mark_pushed(qso_id)
    assert db.pending_push() == []
    assert db.tick_unconfirmed(3) == 0  # attempt 1 → 2 (< retries)
    assert db.tick_unconfirmed(3) == 0  # attempt 2 → 3 (< retries)
    assert db.pending_push() == []
    assert db.tick_unconfirmed(3) == 1  # attempt 3 → requeued
    assert db.pending_push() == [qso_id]


def test_confirm_pushed_clears_pending(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    qso_id = db.insert_record(_record(source="live"))
    db.mark_pushed(qso_id)
    assert db.tick_unconfirmed(3) == 0
    db.confirm_pushed(qso_id)
    assert db.pending_push() == []  # pushed stays 1, nothing queued
    assert db.tick_unconfirmed(3) == 0  # counter cleared → no requeue path


def test_sync_state_roundtrip(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    assert db.state_get("last_seen_pk") is None
    db.state_set("last_seen_pk", "27139")
    assert db.state_get("last_seen_pk") == "27139"


def test_audit_recorded(ft8_db_path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.record_audit("rumlog_pull", "inserted=2 skipped=1")
    rows = db._con.execute("SELECT operation, detail FROM audit_event").fetchall()
    assert [(r["operation"], r["detail"]) for r in rows] == [
        ("rumlog_pull", "inserted=2 skipped=1")
    ]
