"""Pure dedup core: scan, keep, merge, plan, SQL (no real Core Data)."""

from __future__ import annotations

from rumlog_dedup.core import (
    build_plan,
    compute_merge,
    pick_keep,
    render_sql,
    scan_duplicates,
    summarize,
)


def _row(pk, call, band, epoch, grid="", rst=None, freq=None, mode="FT8"):
    return {
        "Z_PK": pk,
        "ZCALLSIGN": call,
        "ZBAND": band,
        "ZDATETIME": epoch,
        "ZLOCATOR": grid,
        "ZRSTTX": rst,
        "ZQRG": freq,
        "ZMODE": mode,
    }


def _pks(clusters):
    return [[r["Z_PK"] for r in c] for c in clusters]


def test_identical_duplicates_collapse():
    rows = [_row(i, "TL8GD", "20m", 100.0) for i in range(5)]
    assert _pks(scan_duplicates(rows)) == [[0, 1, 2, 3, 4]]


def test_empty_band_is_wildcard():
    rows = [_row(1, "X", "", 100.0), _row(2, "X", "20m", 100.0)]
    assert _pks(scan_duplicates(rows)) == [[1, 2]]


def test_different_band_not_duplicate():
    rows = [_row(1, "X", "20m", 100.0), _row(2, "X", "40m", 100.0)]
    assert scan_duplicates(rows) == []


def test_window_boundary_included():
    rows = [_row(1, "X", "20m", 119.0), _row(2, "X", "20m", 121.0)]
    assert _pks(scan_duplicates(rows)) == [[1, 2]]


def test_chain_does_not_span_window():
    # 0,100,200: 0-100 within 120 s, but 0-200 is not -> only {0,100} cluster.
    rows = [
        _row(1, "X", "20m", 0.0),
        _row(2, "X", "20m", 100.0),
        _row(3, "X", "20m", 200.0),
    ]
    assert _pks(scan_duplicates(rows)) == [[1, 2]]


def test_pick_keep_rules():
    cluster = [_row(3, "X", "20m", 90.0), _row(2, "X", "20m", 100.0)]
    assert pick_keep(cluster, "earliest")["Z_PK"] == 3  # 90 s is earliest
    assert pick_keep(cluster, "lowest_zpk")["Z_PK"] == 2  # 2 < 3


def test_merge_fills_complementary_fields():
    keep = _row(1, "X", "20m", 90.0, grid="QF22", rst=None, freq=0)
    dup = _row(2, "X", "20m", 134.0, grid="QF22AB", rst="-12", freq=14.074)
    updates = compute_merge([keep, dup], keep)
    assert updates["ZLOCATOR"] == "QF22AB"  # 6-char beats 4-char
    assert updates["ZRSTTX"] == "-12"
    assert updates["ZQRG"] == 14.074


def test_build_plan_keep_delete_merge():
    keep = _row(1, "X", "20m", 90.0, grid="QF22", rst=None)
    dup = _row(2, "X", "20m", 134.0, grid="QF22", rst="-12")
    plan = build_plan([[keep, dup]], keep_rule="earliest", merge=True)
    assert len(plan) == 1
    p = plan[0]
    assert p["keep_zpk"] == 1
    assert p["delete_zpk"] == [2]
    assert p["merge_updates"] == {"ZRSTTX": "-12"}


def test_summarize():
    plan = build_plan(
        [
            [_row(1, "A", "20m", 100.0), _row(2, "A", "20m", 150.0)],
            [_row(3, "B", "20m", 200.0), _row(4, "B", "20m", 260.0)],
        ],
        merge=False,
    )
    assert summarize(plan) == {
        "clusters": 2,
        "rows_involved": 4,
        "rows_to_delete": 2,
        "merges": 0,
    }


def test_render_sql_contains_transaction_and_delete():
    plan = build_plan(
        [[_row(1, "A", "20m", 100.0, rst=None), _row(2, "A", "20m", 150.0, rst="-12")]],
        merge=True,
    )
    sql = render_sql(plan)
    assert "BEGIN TRANSACTION;" in sql
    assert "COMMIT;" in sql
    assert "UPDATE ZCORE_QSO SET ZRSTTX" in sql
    assert "DELETE FROM ZCORE_QSO WHERE Z_PK IN (2)" in sql
