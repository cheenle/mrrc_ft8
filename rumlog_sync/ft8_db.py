"""FT8 canonical db access for the sync program (spec §8).

The server's Repository keeps its own explicit-column queries, so adding
``rumlog_uuid`` / ``pushed_to_rumlog`` columns is backward compatible.
Migrations are idempotent (PRAGMA probe + ADD COLUMN).  Push state uses a
per-record attempt counter in ``rumlog_sync_state`` (JSON map keyed by qso
id): ``tick_unconfirmed`` ages the counters and requeues records whose
unconfirmed pushes have reached ``confirm_retries``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

PUSH_PENDING_KEY = "push_pending"

# Server-owned qso schema (repository.py) plus the sync columns; only used
# when the canonical db is missing/empty (tests, rebuilds).  The production
# db is created by the server and this only adds columns idempotently.
_QSO_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS qso (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    my_call TEXT NOT NULL,
    my_grid TEXT NOT NULL,
    dx_call TEXT NOT NULL,
    dx_grid TEXT NOT NULL DEFAULT '',
    report_sent INTEGER,
    report_rcvd INTEGER,
    started_utc TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'FT8',
    freq_hz INTEGER NOT NULL DEFAULT 0,
    band TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    completed_epoch REAL NOT NULL,
    void_actor TEXT,
    void_reason TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    rumlog_uuid TEXT NOT NULL DEFAULT '',
    pushed_to_rumlog INTEGER NOT NULL DEFAULT 0
)
"""

_AUDIT_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch REAL NOT NULL,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
)
"""


class Ft8Db:
    """One connection to the FT8 canonical db; caller owns commit/close."""

    def __init__(self, db_path: str | Path) -> None:
        self._con = sqlite3.connect(str(db_path), timeout=10.0)
        self._con.row_factory = sqlite3.Row

    # ---- schema --------------------------------------------------------

    def ensure_schema(self) -> None:
        self._con.execute(_QSO_CREATE_SQL)
        self._con.execute(_AUDIT_CREATE_SQL)
        cols = {r["name"] for r in self._con.execute("PRAGMA table_info(qso)")}
        if "rumlog_uuid" not in cols:
            self._con.execute(
                "ALTER TABLE qso ADD COLUMN rumlog_uuid TEXT NOT NULL DEFAULT ''"
            )
        if "pushed_to_rumlog" not in cols:
            self._con.execute(
                "ALTER TABLE qso ADD COLUMN pushed_to_rumlog INTEGER NOT NULL DEFAULT 0"
            )
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS rumlog_sync_state"
            " (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._con.commit()

    # ---- lookups --------------------------------------------------------

    def find_existing(self, dx_call: str, band: str, epoch: float) -> int | None:
        """qso id matching dx_call+band within the 120 s window, or None."""

        row = self._con.execute(
            "SELECT id FROM qso WHERE dx_call = ? AND band = ?"
            " AND status = 'completed' AND abs(completed_epoch - ?) <= 120"
            " ORDER BY abs(completed_epoch - ?) LIMIT 1",
            (dx_call, band, epoch, epoch),
        ).fetchone()
        return row["id"] if row else None

    def find_by_uuid(self, uuid_hex: str) -> int | None:
        row = self._con.execute(
            "SELECT id FROM qso WHERE rumlog_uuid = ?", (uuid_hex,)
        ).fetchone()
        return row["id"] if row else None

    # ---- writes ---------------------------------------------------------

    def insert_record(self, rec: dict[str, object]) -> int:
        # Records pulled from RUMLogNG already exist there → never re-push.
        pushed = 1 if rec.get("source") == "rumlog" else 0
        cur = self._con.execute(
            "INSERT INTO qso (my_call, my_grid, dx_call, dx_grid, report_sent,"
            " report_rcvd, started_utc, mode, freq_hz, band, status,"
            " completed_epoch, rumlog_uuid, source, pushed_to_rumlog)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec["my_call"], rec["my_grid"], rec["dx_call"], rec["dx_grid"],
                rec["report_sent"], rec["report_rcvd"], rec["started_utc"],
                rec["mode"], rec["freq_hz"], rec["band"], rec["status"],
                rec["completed_epoch"], rec.get("rumlog_uuid", ""),
                rec.get("source", "rumlog"), pushed,
            ),
        )
        lastrowid = cur.lastrowid
        if lastrowid is None:  # pragma: no cover - INSERT always sets rowid
            raise RuntimeError("qso INSERT failed to return a rowid")
        return lastrowid

    def update_from_rumlog(self, qso_id: int, rec: dict[str, object]) -> None:
        """RUMLogNG wins on its fields; completion time fields untouched."""

        self._con.execute(
            "UPDATE qso SET dx_grid = ?, report_sent = ?, report_rcvd = ?,"
            " band = ?, mode = ?, freq_hz = ?, rumlog_uuid = ? WHERE id = ?",
            (
                rec["dx_grid"], rec["report_sent"], rec["report_rcvd"],
                rec["band"], rec["mode"], rec["freq_hz"],
                rec["rumlog_uuid"], qso_id,
            ),
        )

    def get_record(self, qso_id: int) -> dict[str, object] | None:
        row = self._con.execute(
            "SELECT * FROM qso WHERE id = ?", (qso_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- push state machine ----------------------------------------------

    def pending_push(self) -> list[int]:
        rows = self._con.execute(
            "SELECT id FROM qso WHERE pushed_to_rumlog = 0"
            " AND status = 'completed' ORDER BY id"
        ).fetchall()
        return [r["id"] for r in rows]

    def pending_push_records(self) -> list[dict[str, object]]:
        rows = self._con.execute(
            "SELECT id, my_call, my_grid, dx_call, dx_grid, report_sent, report_rcvd,"
            " started_utc, mode, freq_hz, band, completed_epoch FROM qso"
            " WHERE pushed_to_rumlog = 0 AND status = 'completed' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def _pending(self) -> dict[int, int]:
        """Parsed push_pending map {qso_id: attempts}; corrupt state resets."""

        raw = self.state_get(PUSH_PENDING_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:  # JSONDecodeError is a ValueError subclass
            log.warning("corrupt %s state; resetting", PUSH_PENDING_KEY)
            return {}
        out: dict[int, int] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                try:
                    out[int(key)] = int(value)
                except (TypeError, ValueError):
                    continue
        return out

    def _set_pending(self, pending: dict[int, int]) -> None:
        self.state_set(PUSH_PENDING_KEY, json.dumps(pending))

    def mark_pushed(self, qso_id: int) -> None:
        self._con.execute(
            "UPDATE qso SET pushed_to_rumlog = 1 WHERE id = ?", (qso_id,)
        )
        pending = self._pending()
        pending[qso_id] = 1  # fresh unconfirmed push
        self._set_pending(pending)

    def tick_unconfirmed(self, confirm_retries: int = 3) -> int:
        """Age unconfirmed pushes; requeue past the retry budget.

        Called once per round before new pushes: every still-unconfirmed
        record gains an attempt; one that reaches ``confirm_retries`` is
        requeued (``pushed_to_rumlog=0``) so the next push re-sends it.
        Returns the number requeued.
        """

        pending = self._pending()
        requeued = 0
        for qso_id, attempts in list(pending.items()):
            if attempts >= confirm_retries:
                self._con.execute(
                    "UPDATE qso SET pushed_to_rumlog = 0 WHERE id = ?",
                    (qso_id,),
                )
                del pending[qso_id]
                requeued += 1
                log.warning(
                    "push for qso %s unconfirmed after %d rounds; requeued",
                    qso_id, attempts,
                )
            else:
                pending[qso_id] = attempts + 1
        self._set_pending(pending)
        return requeued

    def confirm_pushed(self, qso_id: int) -> None:
        """RUMLogNG now holds this QSO: mark pushed and clear pending."""

        self._con.execute(
            "UPDATE qso SET pushed_to_rumlog = 1 WHERE id = ?", (qso_id,)
        )
        pending = self._pending()
        pending.pop(qso_id, None)
        self._set_pending(pending)

    def rollback(self) -> None:
        self._con.rollback()

    # ---- sync state ------------------------------------------------------

    def state_get(self, key: str) -> str | None:
        row = self._con.execute(
            "SELECT value FROM rumlog_sync_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def state_set(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT INTO rumlog_sync_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def record_audit(self, operation: str, detail: str) -> None:
        self._con.execute(
            "INSERT INTO audit_event (epoch, actor, operation, detail)"
            " VALUES (?, 'rumlog_sync', ?, ?)",
            (time.time(), operation, detail),
        )

    def commit(self) -> None:
        self._con.commit()

    def close(self) -> None:
        self._con.close()
