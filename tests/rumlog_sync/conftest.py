"""Shared fixtures: simulated RUMLogNG Core Data db + scratch dirs."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rumlog_sync.mapper import CORE_DATA_EPOCH_OFFSET

RUMLOG_COLUMNS = """
    Z_PK INTEGER PRIMARY KEY,
    Z_ENT INTEGER NOT NULL DEFAULT 3,
    Z_OPT INTEGER NOT NULL DEFAULT 1,
    ZCOLORCODE INTEGER, ZDXCCADIF INTEGER,
    ZDATETIME TIMESTAMP, ZQRG FLOAT, ZBAND VARCHAR, ZCALLSIGN VARCHAR,
    ZCLUBLOG VARCHAR, ZCNT VARCHAR, ZCOUNTY VARCHAR, ZCQ VARCHAR,
    ZCREDITS VARCHAR, ZDXCC VARCHAR, ZEQSL VARCHAR, ZIOTA VARCHAR,
    ZIOTACREDITS VARCHAR, ZITU VARCHAR, ZLOCATOR VARCHAR, ZLOTWQSL VARCHAR,
    ZMANAGER VARCHAR, ZMODE VARCHAR, ZNAME VARCHAR, ZNOTE VARCHAR,
    ZPOWER VARCHAR, ZPREFIX VARCHAR, ZQSL VARCHAR, ZQSLINDATE VARCHAR,
    ZQSLOUTDATE VARCHAR, ZQTH VARCHAR, ZRSTRX VARCHAR, ZRSTTX VARCHAR,
    ZSATMODE VARCHAR, ZSATNAME VARCHAR, ZSATRXBAND VARCHAR, ZSTATE VARCHAR,
    ZUSER_1 VARCHAR, ZUSER_2 VARCHAR, ZUSER_3 VARCHAR, ZUSER_4 VARCHAR,
    ZUUID BLOB
"""


# Column names accepted from test row dicts (identifier allowlist).
_ALLOWED_COLUMNS = frozenset(
    {
        "unix_epoch",  # test-only; rewritten to ZDATETIME below
        "Z_PK", "Z_ENT", "Z_OPT", "ZCOLORCODE", "ZDXCCADIF", "ZDATETIME",
        "ZQRG", "ZBAND", "ZCALLSIGN", "ZCLUBLOG", "ZCNT", "ZCOUNTY",
        "ZCQ", "ZCREDITS", "ZDXCC", "ZEQSL", "ZIOTA", "ZIOTACREDITS",
        "ZITU", "ZLOCATOR", "ZLOTWQSL", "ZMANAGER", "ZMODE", "ZNAME",
        "ZNOTE", "ZPOWER", "ZPREFIX", "ZQSL", "ZQSLINDATE", "ZQSLOUTDATE",
        "ZQTH", "ZRSTRX", "ZRSTTX", "ZSATMODE", "ZSATNAME", "ZSATRXBAND",
        "ZSTATE", "ZUSER_1", "ZUSER_2", "ZUSER_3", "ZUSER_4", "ZUUID",
    }
)


def _make_rumlog_db(path: Path, rows: list[dict[str, Any]]) -> None:
    """Create a CoreQsoModel_1.sqlite-shaped db; rows use unix-style ZDATETIME.

    Callers pass ``unix_epoch`` values; the helper converts to Core Data
    seconds so mapper tests stay readable.
    """

    con = sqlite3.connect(path)
    con.execute("DROP TABLE IF EXISTS ZCORE_QSO")
    con.execute(f"CREATE TABLE ZCORE_QSO ({RUMLOG_COLUMNS})")
    for row in rows:
        data = dict(row)
        if "unix_epoch" in data:
            data["ZDATETIME"] = float(data.pop("unix_epoch")) - CORE_DATA_EPOCH_OFFSET
        unknown = set(data) - _ALLOWED_COLUMNS
        if unknown:
            raise ValueError(f"unexpected row column(s): {sorted(unknown)}")
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        con.execute(
            f"INSERT INTO ZCORE_QSO ({cols}) VALUES ({marks})",
            tuple(data.values()),
        )
    con.commit()
    con.close()


@pytest.fixture
def make_rumlog_db():
    """Fixture exposing the simulated-RUMLogNG-db builder."""

    return _make_rumlog_db


@pytest.fixture
def sample_rumlog_row() -> dict[str, object]:
    return {
        "ZCALLSIGN": "TL8GD",
        "unix_epoch": 1_795_384_049.0,  # 2026-08-10 23:23:29 UTC
        "ZQRG": 14.074684,
        "ZBAND": "20m",
        "ZMODE": "FT8",
        "ZLOCATOR": "",
        "ZRSTTX": "-12",
        "ZRSTRX": "-12",
        "ZUUID": bytes.fromhex("C9DE3F473B90413DA8F6049CDFD26A3B"),
    }


@pytest.fixture
def ft8_db_path(tmp_path: Path) -> Path:
    return tmp_path / "mrrc-ft8.db"
