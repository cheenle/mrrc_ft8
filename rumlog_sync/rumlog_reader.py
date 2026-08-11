"""Read-only access to RUMLogNG's Core Data SQLite store.

Spec §3.2: opened with ``uri mode=ro`` so the running app is never
disturbed; WAL makes concurrent reads safe.  A column existence probe
guards against RUMLogNG schema changes (fail loudly, never guess).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = (
    "Z_PK",
    "ZCALLSIGN",
    "ZDATETIME",
    "ZQRG",
    "ZBAND",
    "ZMODE",
    "ZLOCATOR",
    "ZRSTTX",
    "ZRSTRX",
    "ZUUID",
)


class SchemaMismatch(RuntimeError):
    """RUMLogNG database lacks a column this sync depends on."""


class RUMlogReader:
    """One read-only connection; fetch new rows past a Z_PK cursor."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        try:
            self._con = sqlite3.connect(
                f"file:{self._db_path}?mode=ro", uri=True, timeout=5.0
            )
        except sqlite3.Error as exc:
            # sqlite3.OperationalError is not an OSError subclass; normalize
            # so callers can treat open failures uniformly (spec §7).
            raise OSError(f"cannot open RUMLogNG db {self._db_path}: {exc}") from exc
        self._con.row_factory = sqlite3.Row
        self._verify_columns()

    def _verify_columns(self) -> None:
        present = {
            row["name"]
            for row in self._con.execute("PRAGMA table_info(ZCORE_QSO)").fetchall()
        }
        missing = [c for c in REQUIRED_COLUMNS if c not in present]
        if missing:
            raise SchemaMismatch(
                f"RUMLogNG schema missing columns: {missing} (path {self._db_path})"
            )

    def fetch_since(self, last_pk: int) -> list[dict[str, object]]:
        """Rows with Z_PK > last_pk, ascending; raw column values."""

        rows = self._con.execute(
            "SELECT Z_PK, ZCALLSIGN, ZDATETIME, ZQRG, ZBAND, ZMODE,"
            " ZLOCATOR, ZRSTTX, ZRSTRX, ZUUID FROM ZCORE_QSO"
            " WHERE Z_PK > ? ORDER BY Z_PK ASC",
            (last_pk,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._con.close()
