"""rumlog_reader: read-only Core Data access, column probe, Z_PK cursor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rumlog_sync.rumlog_reader import RUMlogReader, SchemaMismatch


def _rows(sample: dict[str, object]) -> list[dict[str, object]]:
    rows = []
    for pk, suffix in ((27137, ""), (27139, "B")):
        row = dict(sample)
        row["Z_PK"] = pk
        row["ZCALLSIGN"] = "TL8GD" + suffix
        rows.append(row)
    return rows


def test_reader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        RUMlogReader(tmp_path / "missing.sqlite")


def test_reader_detects_missing_columns(tmp_path: Path, sample_rumlog_row) -> None:
    db = tmp_path / "bad.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ZCORE_QSO (Z_PK INTEGER PRIMARY KEY, ZCALLSIGN VARCHAR)")
    con.commit()
    con.close()
    with pytest.raises(SchemaMismatch):
        RUMlogReader(db)


def test_fetch_since_returns_only_new_rows(
    tmp_path: Path, sample_rumlog_row, make_rumlog_db
) -> None:
    db = tmp_path / "log.sqlite"
    make_rumlog_db(db, _rows(sample_rumlog_row))
    reader = RUMlogReader(db)
    first = reader.fetch_since(0)
    assert [r["Z_PK"] for r in first] == [27137, 27139]
    assert first[0]["ZCALLSIGN"] == "TL8GD"
    # ZDATETIME comes back as raw Core Data seconds; mapper converts.
    again = reader.fetch_since(27138)
    assert [r["Z_PK"] for r in again] == [27139]
    assert reader.fetch_since(27139) == []
