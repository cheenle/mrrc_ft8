"""SQLite canonical persistence: QSO, audit, decode history, settings.

AD-014 / §7.5: SQLite is the canonical store; ADIF is generated from it.
NFR-071 retention (decode history 7 d, audit 90 d, QSO indefinite) is
enforced by :meth:`Repository.enforce_retention`.  NFR-073 audited undo is
:meth:`Repository.void_qso` — it never deletes, it marks void with
actor/time/reason evidence (§6: within 30 s of automatic completion).
NFR-058 startup/shutdown marks interrupted QSOs ``ABORTED_RESTART`` via
:meth:`Repository.abort_active_qsos`.

All methods are short synchronous transactions guarded by one lock; the
async engine calls them through ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .sequencer import QSORecord

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
UNDO_WINDOW_S = 30.0  # §6: automatic completion can be undone for 30 s
DECODE_RETENTION_S = 7 * 86_400  # NFR-071
AUDIT_RETENTION_S = 90 * 86_400  # NFR-071


class QsoStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABORTED_RESTART = "aborted_restart"
    VOID = "void"


@dataclass(frozen=True, slots=True)
class StoredQSO:
    """One canonical QSO row; void rows stay auditable (§7.4)."""

    id: int
    my_call: str
    my_grid: str
    dx_call: str
    dx_grid: str
    report_sent: int | None
    report_rcvd: int | None
    started_utc: str
    mode: str
    freq_hz: int
    band: str
    status: QsoStatus
    completed_epoch: float
    void_actor: str | None
    void_reason: str | None


class VoidWindowExpired(Exception):
    """Undo arrived after the 30 s post-completion window (§6)."""


_SCHEMA = """
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
    void_reason TEXT
);
CREATE TABLE IF NOT EXISTS qso_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qso_id INTEGER NOT NULL REFERENCES qso(id),
    epoch REAL NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch REAL NOT NULL,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS decode_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch REAL NOT NULL,
    slot_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    snr_db INTEGER
);
CREATE TABLE IF NOT EXISTS setting_meta (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
"""

_QSO_COLUMNS = (
    "id, my_call, my_grid, dx_call, dx_grid, report_sent, report_rcvd,"
    " started_utc, mode, freq_hz, band, status, completed_epoch,"
    " void_actor, void_reason"
)


class Repository:
    """Thread-safe SQLite store; one instance per database file."""

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._open()
        self._migrate()

    def _open(self) -> None:
        """Open (or reopen) the sqlite connection for ``self._path``."""

        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")

    def _reopen_after_dbmoved(self) -> None:
        """Recover from SQLite's DBMOVED guard (file replaced underneath us).

        When an external process replaces the db file mid-run, SQLite refuses
        every write on the stale connection with ``OperationalError: attempt
        to write a readonly database`` (SQLITE_READONLY_DBMOVED).  The fix is
        to drop the stale connection and reopen the (new) file at the same
        path, then re-run migrations so the schema follows the file.  All
        write entry points call this and retry once.
        """

        log.warning("repository: db file replaced on disk — reopening %s", self._path)
        try:
            self._db.close()
        except Exception:
            pass  # stale connection may already be broken
        self._open()
        # Already inside ``self._lock`` (called from ``_write``) — migrate
        # without re-acquiring the non-reentrant lock.
        self._migrate_locked()

    @staticmethod
    def _is_dbmoved(exc: sqlite3.OperationalError) -> bool:
        """True when SQLite reports the file was replaced (readonly guard)."""

        return "readonly database" in str(exc)

    def close(self) -> None:
        """Close the database; idempotent."""

        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None  # type: ignore[assignment]

    def _migrate(self) -> None:
        with self._lock:
            self._migrate_locked()

    def _migrate_locked(self) -> None:
        """Schema check/migration; caller must hold ``self._lock``."""

        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than supported")
        if version < SCHEMA_VERSION:
            with self._db:
                self._db.executescript(_SCHEMA)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _write(self, fn: Callable[[], object]) -> object:
        """Run one write, transparently recovering from DBMOVED once.

        SQLite refuses writes after an external process replaces the db file
        (SQLITE_READONLY_DBMOVED).  We reopen the new file and retry exactly
        once; a second failure propagates to the caller (real disk problem).
        """

        with self._lock:
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if not self._is_dbmoved(exc):
                    raise
                self._reopen_after_dbmoved()
                return fn()

    # ---- QSO ------------------------------------------------------------

    def record_qso(
        self,
        record: QSORecord,
        *,
        status: QsoStatus = QsoStatus.COMPLETED,
        completed_epoch: float | None = None,
    ) -> int:
        """Insert one QSO (from the sequencer's log record); returns its id."""

        epoch = self._clock() if completed_epoch is None else completed_epoch

        def _insert() -> int:
            with self._db:
                cursor = self._db.execute(
                    "INSERT INTO qso (my_call, my_grid, dx_call, dx_grid, report_sent,"
                    " report_rcvd, started_utc, mode, freq_hz, band, status,"
                    " completed_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.my_call,
                        record.my_grid,
                        record.dx_call,
                        record.dx_grid,
                        record.report_sent,
                        record.report_rcvd,
                        record.started_utc,
                        record.mode,
                        record.freq_hz,
                        record.band,
                        status.value,
                        epoch,
                    ),
                )
                qso_id = int(cursor.lastrowid)
                self._qso_event(qso_id, epoch, status.value, "recorded")
                return qso_id

        return int(self._write(_insert))  # type: ignore[return-value]

    def list_qsos(self, *, include_void: bool = True) -> list[StoredQSO]:
        """All QSOs newest-first; ADIF export passes ``include_void=False``."""

        query = f"SELECT {_QSO_COLUMNS} FROM qso"
        if not include_void:
            query += " WHERE status != ?"
        query += " ORDER BY id DESC"
        with self._lock:
            rows = (
                self._db.execute(query, (QsoStatus.VOID.value,)).fetchall()
                if not include_void
                else self._db.execute(query).fetchall()
            )
        return [self._to_stored(row) for row in rows]

    def get_qso(self, qso_id: int) -> StoredQSO | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_QSO_COLUMNS} FROM qso WHERE id = ?", (qso_id,)
            ).fetchone()
        return self._to_stored(row) if row is not None else None

    def void_qso(self, qso_id: int, *, actor: str, reason: str) -> None:
        """Audited undo within 30 s of completion (NFR-073, §6).

        Never deletes; marks void and records actor/time/reason in both the
        QSO trail and the audit log.
        """

        now = self._clock()

        def _void() -> None:
            with self._db:
                row = self._db.execute(
                    "SELECT status, completed_epoch FROM qso WHERE id = ?", (qso_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"no QSO {qso_id}")
                if row["status"] != QsoStatus.COMPLETED.value:
                    raise ValueError(f"only a completed QSO can be voided (got {row['status']})")
                if now - row["completed_epoch"] > UNDO_WINDOW_S:
                    raise VoidWindowExpired(
                        f"undo window of {UNDO_WINDOW_S:.0f} s has expired"
                    )
                self._db.execute(
                    "UPDATE qso SET status = ?, void_actor = ?, void_reason = ? WHERE id = ?",
                    (QsoStatus.VOID.value, actor, reason, qso_id),
                )
                self._qso_event(qso_id, now, QsoStatus.VOID.value, f"{actor}: {reason}")
                self._audit_locked(now, actor, "qso_void", f"qso:{qso_id}", reason)

        self._write(_void)

    def abort_active_qsos(self, *, reason: str = "ABORTED_RESTART") -> int:
        """Mark every in-progress QSO aborted at shutdown/startup (NFR-058)."""

        now = self._clock()

        def _abort() -> int:
            with self._db:
                rows = self._db.execute(
                    "SELECT id FROM qso WHERE status = ?", (QsoStatus.ACTIVE.value,)
                ).fetchall()
                for row in rows:
                    self._db.execute(
                        "UPDATE qso SET status = ? WHERE id = ?",
                        (QsoStatus.ABORTED_RESTART.value, row["id"]),
                    )
                    self._qso_event(row["id"], now, QsoStatus.ABORTED_RESTART.value, reason)
                return len(rows)

        return int(self._write(_abort))  # type: ignore[return-value]

    # ---- audit / decode history ------------------------------------------

    def record_audit(
        self, *, actor: str, operation: str, target: str = "", detail: str = ""
    ) -> None:
        """One audit trail entry (§10: actor, operation, target, result)."""

        def _record() -> None:
            with self._db:
                self._audit_locked(self._clock(), actor, operation, target, detail)

        self._write(_record)

    def record_decode_event(
        self, *, slot_id: int, message: str, snr_db: int | None = None
    ) -> None:
        """One decode-history line; retained for 7 days (NFR-071)."""

        def _record() -> None:
            with self._db:
                self._db.execute(
                    "INSERT INTO decode_event (epoch, slot_id, message, snr_db)"
                    " VALUES (?,?,?,?)",
                    (self._clock(), slot_id, message, snr_db),
                )

        self._write(_record)

    def count_rows(self, table: str) -> int:
        """Row count for retention regressions and health (allow-listed)."""

        if table not in {"qso", "qso_event", "audit_event", "decode_event", "setting_meta"}:
            raise ValueError(f"unknown table {table!r}")
        with self._lock:
            return int(self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def enforce_retention(self, *, now: float | None = None) -> dict[str, int]:
        """Delete expired decode/audit rows; QSO data never expires."""

        now = self._clock() if now is None else now

        def _retain() -> dict[str, int]:
            with self._db:
                decode = self._db.execute(
                    "DELETE FROM decode_event WHERE epoch < ?", (now - DECODE_RETENTION_S,)
                ).rowcount
                audit = self._db.execute(
                    "DELETE FROM audit_event WHERE epoch < ?", (now - AUDIT_RETENTION_S,)
                ).rowcount
            return {"decode_event": int(decode), "audit_event": int(audit)}

        return dict(self._write(_retain))  # type: ignore[return-value]

    # ---- settings ---------------------------------------------------------

    def set_setting(self, key: str, value: object) -> None:
        """Persist one JSON-serializable setting (§12.6 restore on restart)."""

        def _set() -> None:
            with self._db:
                self._db.execute(
                    "INSERT INTO setting_meta (key, value_json) VALUES (?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                    (key, json.dumps(value)),
                )

        self._write(_set)

    def get_setting(self, key: str, default: object = None) -> object:
        with self._lock:
            row = self._db.execute(
                "SELECT value_json FROM setting_meta WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row["value_json"])

    # ---- internals ---------------------------------------------------------

    def _qso_event(self, qso_id: int, epoch: float, event: str, detail: str) -> None:
        self._db.execute(
            "INSERT INTO qso_event (qso_id, epoch, event, detail) VALUES (?,?,?,?)",
            (qso_id, epoch, event, detail),
        )

    def _audit_locked(
        self, epoch: float, actor: str, operation: str, target: str, detail: str
    ) -> None:
        self._db.execute(
            "INSERT INTO audit_event (epoch, actor, operation, target, detail)"
            " VALUES (?,?,?,?,?)",
            (epoch, actor, operation, target, detail),
        )

    def audit_events(self, *, limit: int = 1_000) -> list[dict[str, object]]:
        """Newest audit rows for the diagnostic export (§10.6, NFR-075).

        Actor values are session id prefixes, never cookie secrets.
        """

        with self._lock:
            rows = self._db.execute(
                "SELECT epoch, actor, operation, target, detail FROM audit_event"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def qso_events(self, qso_id: int) -> list[tuple[float, str, str]]:
        """Trail of one QSO for audit display (epoch, event, detail)."""

        with self._lock:
            rows = self._db.execute(
                "SELECT epoch, event, detail FROM qso_event WHERE qso_id = ? ORDER BY id",
                (qso_id,),
            ).fetchall()
        return [(float(r["epoch"]), r["event"], r["detail"]) for r in rows]

    @staticmethod
    def _to_stored(row: sqlite3.Row) -> StoredQSO:
        return StoredQSO(
            id=int(row["id"]),
            my_call=row["my_call"],
            my_grid=row["my_grid"],
            dx_call=row["dx_call"],
            dx_grid=row["dx_grid"],
            report_sent=row["report_sent"],
            report_rcvd=row["report_rcvd"],
            started_utc=row["started_utc"],
            mode=row["mode"],
            freq_hz=int(row["freq_hz"]),
            band=row["band"],
            status=QsoStatus(row["status"]),
            completed_epoch=float(row["completed_epoch"]),
            void_actor=row["void_actor"],
            void_reason=row["void_reason"],
        )
