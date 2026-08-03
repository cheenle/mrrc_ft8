# ADIF 日志可靠性 + 元数据完善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three completed-QSO loss paths (failed write after destructive pop, `_reset_partner` wipe before the 1 s poll, restart window) via a durable `QsoLog` queue, and capture freq/band/start-time metadata so ADIF exports real `FREQ`/`BAND`/`TIME_ON`/`TIME_OFF`.

**Architecture:** The sequencer fires an injected `on_qso` callback the instant a QSO completes, so the record leaves sequencer mutable state immediately and `reply_to`/`start_cq` can never destroy it. A new `QsoLog` component owns the pending queue; the composition watchdog drains at most one record per tick, retries persistent failures up to `max_attempts`, then spills to a `data/qso-pending.jsonl` dead-letter file that `recover()` re-reads on startup and `flush()` drains on shutdown. Metadata (freq/band/start) is captured at QSO start via injected `context`/`clock`.

**Tech Stack:** Python 3 (asyncio, FastAPI), SQLite via `server/engine/repository.py`, pytest. No new dependencies.

## Global Constraints

- Python code follows the repo conventions (AGENTS.md): asyncio; blocking hardware/DB I/O via `asyncio.to_thread`; type annotations; Google-style docstrings.
- `server/engine/sequencer.py` stays hardware-agnostic — freq/band arrive via injected callables, never by importing radio code.
- Enqueue/drain/flush of `QsoLog` run on the event loop thread; the only blocking call (`record_qso`) is offloaded with `asyncio.to_thread`.
- The 30 s void window (NFR-073) and non-void-only ADIF export (NFR-072) are unchanged.
- Every code change syncs the matching SDD chapter + `SDD/14-version-history.md` (AGENTS.md铁律; see sdd-guardian skill) — handled in Task 6.
- No new runtime dependencies. The `data/` directory is already gitignored, so `data/qso-pending.jsonl` needs no `.gitignore` change.

---
## File Structure

- **Create** `server/engine/bands.py` — FT8 dial freq → ADIF band name (server-side mirror of `web/static/js/band.js`).
- **Modify** `server/engine/sequencer.py` — replace `_log_ready`/`pop_log_record` with `on_qso` callback + `context`/`clock` injection + QSO start-time capture.
- **Rewrite** `server/engine/qso_log.py` — `QsoLog` durable queue component (replaces the one-shot `record_qso` helper).
- **Modify** `server/engine/adif.py` — `TIME_ON` from `started_utc` with completion fallback; add `TIME_OFF`.
- **Modify** `server/web/api.py` — `AppState.qso_log` field; health `qso_pending`.
- **Modify** `server/main.py` — wire `QsoLog` into the composition root; watchdog `drain_once`; startup `recover()`; shutdown `flush()`.
- **Tests** — new `tests/engine/test_bands.py`; rewrite `tests/engine/test_qso_log.py`; extend `tests/engine/test_sequencer.py`, `tests/engine/test_adif.py`, `tests/web/test_api.py`.
- **Docs** — `SDD/07-subject-area-model.md`, `SDD/11-component-model.md`, `SDD/14-version-history.md`, `AGENTS.md`.

---

### Task 1: `server/engine/bands.py` — FT8 freq → ADIF band

**Files:**
- Create: `server/engine/bands.py`
- Test: `tests/engine/test_bands.py`

**Interfaces:**
- Produces: `FT8_BANDS: list[tuple[int, str]]` and `band_from_freq_hz(freq_hz: int) -> str` (empty string when off-band). Used by Task 2's tests (via `QsoContext`) and Task 4's composition wiring.

- [ ] **Step 1: Write the failing test**

Create `tests/engine/test_bands.py`:

```python
"""FT8 dial frequency → ADIF band name mapping (mirror of band.js)."""

from __future__ import annotations

from server.engine.bands import band_from_freq_hz


def test_known_ft8_bands() -> None:
    assert band_from_freq_hz(7_074_000) == "40m"
    assert band_from_freq_hz(14_074_000) == "20m"
    assert band_from_freq_hz(21_074_000) == "15m"
    assert band_from_freq_hz(28_074_000) == "10m"


def test_within_match_tolerance_counts_as_same_band() -> None:
    # band.js: Math.abs(dial - freq) < 50_000 → same band.
    assert band_from_freq_hz(14_074_000 - 49_999) == "20m"
    assert band_from_freq_hz(14_074_000 + 49_999) == "20m"


def test_exactly_at_match_tolerance_is_off_band() -> None:
    assert band_from_freq_hz(14_074_000 - 50_000) == ""
    assert band_from_freq_hz(14_074_000 + 50_000) == ""


def test_off_band_and_zero() -> None:
    assert band_from_freq_hz(1_000_000) == ""
    assert band_from_freq_hz(0) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/engine/test_bands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.engine.bands'`.

- [ ] **Step 3: Write the implementation**

Create `server/engine/bands.py`:

```python
"""FT8 sub-band dial frequencies → ADIF band names.

Server-side mirror of the PWA band selector (``web/static/js/band.js``), so a
completed QSO's dial frequency becomes the ADIF ``BAND`` value.  ADIF band
names (``40m``/``20m``/``15m``/``10m``) follow the WSJT-X export convention.
"""

from __future__ import annotations

# Dial frequencies for the FT8 sub-band on each HF band (matches band.js).
FT8_BANDS: list[tuple[int, str]] = [
    (7_074_000, "40m"),
    (14_074_000, "20m"),
    (21_074_000, "15m"),
    (28_074_000, "10m"),
]

MATCH_HZ = 50_000  # within ±50 kHz counts as the same band (band.js)


def band_from_freq_hz(freq_hz: int) -> str:
    """ADIF band name for an FT8 dial frequency, or ``""`` when off-band."""

    for dial, band in FT8_BANDS:
        if abs(dial - freq_hz) < MATCH_HZ:
            return band
    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/engine/test_bands.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/engine/bands.py tests/engine/test_bands.py
git commit -m "feat(log): FT8 dial frequency → ADIF band name helper"
```

---

### Task 2: Sequencer fires `on_qso` at completion (replaces `pop_log_record`)

**Files:**
- Modify: `server/engine/sequencer.py`
- Test: `tests/engine/test_sequencer.py`, `tests/engine/test_qso_log.py`

**Interfaces:**
- Consumes: Task 1's `band_from_freq_hz` is **not** imported here (sequencer stays hardware-agnostic); freq/band arrive via the injected `context` callable.
- Produces:
  - `QsoContext` dataclass with `freq_hz: int = 0` and `band: str = ""`.
  - `Sequencer` new fields: `on_qso: Callable[[QSORecord], None] | None = None`, `context: Callable[[], QsoContext]`, `clock: Callable[[], float]`.
  - `Sequencer._ensure_log()` now builds the record (with `started_utc` from the captured start epoch and freq/band from `context()`) and calls `on_qso` exactly once per QSO.
  - Removed: `Sequencer.pop_log_record()` and `Sequencer._log_ready`.

- [ ] **Step 1: Write the failing tests**

In `tests/engine/test_sequencer.py` the helper is `make()` (returns `Sequencer(my_call=MY_CALL, my_grid=MY_GRID)`), constants are `MY_CALL="N0CALL"`, `DX="K1ABC"`, and `feed(seq, text, snr_db)` wraps `on_message(parse_message(text))`. The two existing completion tests (`test_cq_side_full_qso` and `test_answerer_full_qso_logs_on_rr73_and_finishes_after_one_73`) call `seq.pop_log_record(...)` — Task 2 deletes that method, so migrate them to the new `on_qso` collector contract.

1. Change the import block to add `QSORecord`:

```python
from server.engine.sequencer import (
    DisarmReason,
    QSOState,
    QSORecord,
    Sequencer,
)
```

2. Change the `make()` helper to forward optional kwargs:

```python
def make(**kwargs: object) -> Sequencer:
    return Sequencer(my_call=MY_CALL, my_grid=MY_GRID, **kwargs)  # type: ignore[arg-type]
```

3. Rewrite the tail of `test_cq_side_full_qso` (lines 46-55) — replace the `pop_log_record` assertions with a collector captured at the top of the test:

```python
def test_cq_side_full_qso() -> None:
    captured: list[QSORecord] = []
    seq = make(
        clock=lambda: 1_700_000_000.0,  # 2023-11-14T22:13:20Z
        context=lambda: QsoContext(freq_hz=14_074_000, band="20m"),
    )
    seq.on_qso = captured.append
    seq.start_cq()
    assert seq.state == QSOState.CALLING
    # CQ repeats without a retransmission budget (UC-004).
    for _ in range(10):
        assert seq.next_tx_message() == "CQ N0CALL FN42"

    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    assert seq.state == QSOState.REPORT
    assert seq.dx_call == DX
    assert seq.dx_grid == "FN42"
    assert seq.next_tx_message() == "K1ABC N0CALL -12"

    feed(seq, "N0CALL K1ABC R-05")
    assert seq.state == QSOState.ROGERS
    assert seq.next_tx_message() == "K1ABC N0CALL RR73"

    feed(seq, "N0CALL K1ABC 73")
    assert seq.state == QSOState.DONE
    assert seq.disarm_reason == DisarmReason.COMPLETE
    assert seq.next_tx_message() is None

    assert len(captured) == 1
    record = captured[0]
    assert record.dx_call == DX
    assert record.dx_grid == "FN42"
    assert record.report_sent == -12
    assert record.report_rcvd == -5
    assert record.started_utc == "221320"
    assert record.freq_hz == 14_074_000
    assert record.band == "20m"
```

4. Rewrite the tail of `test_answerer_full_qso_logs_on_rr73_and_finishes_after_one_73` (lines 77-80) — capture via the collector:

```python
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-15)
    assert seq.state == QSOState.REPLYING
    assert seq.next_tx_message() == "K1ABC N0CALL FN42"

    feed(seq, "N0CALL K1ABC -09")
    assert seq.state == QSOState.ROGER_REPORT
    assert seq.next_tx_message() == "K1ABC N0CALL R-15"

    feed(seq, "N0CALL K1ABC RR73")
    assert seq.state == QSOState.SIGNOFF
    # UC-005: RR73 already completes and logs the QSO.
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    # Partner stayed silent: the logged QSO is over.
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE
    assert seq.disarm_reason == DisarmReason.COMPLETE

    assert len(captured) == 1
    assert captured[0].report_sent == -15
    assert captured[0].report_rcvd == -9
```

5. Add `QsoContext` to the imports and the three new tests at the end of the file:

```python
from server.engine.sequencer import QsoContext  # add to the import block


def drive_cq_qso(sequencer: Sequencer) -> None:
    """CQ side full exchange mirroring test_cq_side_full_qso (ends DONE)."""
    sequencer.start_cq()
    feed(sequencer, "N0CALL K1ABC FN42", snr_db=-12)
    feed(sequencer, "N0CALL K1ABC R-05", snr_db=-5)
    feed(sequencer, "N0CALL K1ABC 73", snr_db=-5)


def test_completed_qso_fires_on_qso_exactly_once() -> None:
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    drive_cq_qso(seq)
    assert len(captured) == 1
    assert captured[0].dx_call == "K1ABC"


def test_log_record_survives_immediate_reply_reset() -> None:
    # The observed bug: reply_to()/_reset_partner() wiped a completed-but-
    # unlogged record.  With on_qso fired at completion the record is already
    # out of sequencer state.
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    drive_cq_qso(seq)
    seq.reply_to(parse_message("CQ W1AW FN42"), snr_db=-10)  # new QSO
    assert len(captured) == 1
    assert captured[0].dx_call == "K1ABC"


def test_log_record_carries_start_time_and_context() -> None:
    captured: list[QSORecord] = []
    seq = make(
        clock=lambda: 1_700_000_000.0,  # 2023-11-14T22:13:20Z
        context=lambda: QsoContext(freq_hz=14_074_000, band="20m"),
    )
    seq.on_qso = captured.append
    drive_cq_qso(seq)
    record = captured[0]
    assert record.started_utc == "221320"
    assert record.freq_hz == 14_074_000
    assert record.band == "20m"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/engine/test_sequencer.py -q`
Expected: FAIL — `Sequencer.__init__()` got unexpected keyword arguments `clock`/`context`, and `on_qso` is not an attribute.

- [ ] **Step 3: Implement the sequencer changes**

Edit `server/engine/sequencer.py`:

1. Add imports at the top:

```python
import time
from collections.abc import Callable
```

2. Add the `QsoContext` dataclass just before the `QSORecord` dataclass:

```python
@dataclass(frozen=True, slots=True)
class QsoContext:
    """Radio context the sequencer records into a completed QSO.

    Injected by the composition root (keeps the sequencer hardware-agnostic);
    ``freq_hz``/``band`` are the dial frequency and ADIF band at QSO start.
    """

    freq_hz: int = 0
    band: str = ""
```

3. Extend the `Sequencer` dataclass fields (insert after `tx_phase`, keeping the existing default ordering):

```python
    clock: Callable[[], float] = time.time
    context: Callable[[], QsoContext] = lambda: QsoContext()
    on_qso: Callable[[QSORecord], None] | None = None
    _qso_logged: bool = field(default=False, repr=False)
    _qso_started_epoch: float | None = field(default=None, repr=False)
```

Replace the existing `_log_ready: QSORecord | None = field(default=None, repr=False)` line with those five.

4. In `reply_to`, after `self._reset_partner()` add the start-time snapshot:

```python
        self._reset_partner()
        self._qso_started_epoch = self.clock()
        self.tx_phase = tx_phase
```

5. In `on_message`, the `CASE CALLING` answer branch (`if msg.from_call:` at the `CALLING` case) add the start-time snapshot:

```python
            case QSOState.CALLING:
                # Any addressed reply answers the CQ.
                if msg.from_call:
                    self.dx_call = msg.from_call
                    self.report_sent = snr_db
                    self._qso_started_epoch = self.clock()
                    self.state = QSOState.REPORT
```

6. Rewrite `_ensure_log`:

```python
    def _ensure_log(self) -> None:
        # UC-005: fire the completed record exactly once, immediately out of
        # sequencer state so partner resets cannot lose it.
        if self._qso_logged:
            return
        self._qso_logged = True
        ctx = self.context()
        started = self._qso_started_epoch
        record = QSORecord(
            my_call=self.my_call,
            my_grid=self.my_grid,
            dx_call=self.dx_call,
            dx_grid=self.dx_grid,
            report_sent=self.report_sent,
            report_rcvd=self.report_rcvd,
            started_utc=(
                time.strftime("%H%M%S", time.gmtime(started))
                if started is not None
                else ""
            ),
            freq_hz=ctx.freq_hz,
            band=ctx.band,
        )
        if self.on_qso is not None:
            self.on_qso(record)
```

7. Delete the `pop_log_record` method entirely.

8. In `_reset_partner`, reset the new fields alongside the existing ones:

```python
    def _reset_partner(self) -> None:
        self.dx_call = ""
        self.dx_grid = ""
        self.report_sent = None
        self.report_rcvd = None
        self.disarm_reason = None
        self.tx_phase = 0  # CQ and unknown-slot replies default to even
        self._tx_count = 0
        self._signoff_sent = False
        self._qso_logged = False
        self._qso_started_epoch = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/engine/test_sequencer.py tests/engine/test_qso_log.py -q`
Expected: the existing `test_qso_log.py` will FAIL to import `record_qso` (deleted next task) — that is expected. Run only the sequencer file for a green signal:

Run: `venv/bin/python -m pytest tests/engine/test_sequencer.py -q`
Expected: PASS (existing 17 + new 3). The `test_qso_log.py` import failure is resolved in Task 3.

- [ ] **Step 5: Commit**

```bash
git add server/engine/sequencer.py tests/engine/test_sequencer.py
git commit -m "feat(log): sequencer fires on_qso once at completion, captures start/band context"
```

---

### Task 3: `QsoLog` durable queue (retry + dead-letter + recover + flush)

**Files:**
- Rewrite: `server/engine/qso_log.py`
- Test: `tests/engine/test_qso_log.py`

**Interfaces:**
- Consumes: `QSORecord` from `sequencer.py` (unchanged shape), `Repository.record_qso(record) -> int` (duck-typed — tests may substitute a stub).
- Produces:
  - `QsoLog(repository, *, pending_path="data/qso-pending.jsonl", max_attempts=5, clock=time.time)`.
  - `enqueue(record: QSORecord) -> None` (sync; event loop thread).
  - `async drain_once() -> None` (at most one record per tick; retries, then spills to the dead-letter file after `max_attempts`).
  - `recover() -> int` (sync; reads journal into the queue and clears it).
  - `async flush() -> None` (best-effort persist of queue + journal, then clear journal).
  - `pending: int` property.

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/engine/test_qso_log.py`:

```python
"""QsoLog durable queue regressions: retry, dead-letter, recover, flush."""

from __future__ import annotations

import asyncio
import sqlite3

from server.engine.qso_log import QsoLog
from server.engine.repository import Repository
from server.engine.sequencer import QSORecord


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def record(dx_call: str = "K1ABC") -> QSORecord:
    return QSORecord(
        my_call="M0XX",
        my_grid="IO91",
        dx_call=dx_call,
        dx_grid="FN42",
        report_sent=-12,
        report_rcvd=-8,
        started_utc="221320",
        mode="FT8",
        freq_hz=14_074_000,
        band="20m",
    )


class FlakyRepo:
    """record_qso fails the first ``fail_first`` calls, then succeeds."""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.written: list[QSORecord] = []

    def record_qso(self, qso: QSORecord) -> int:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise sqlite3.OperationalError("database is locked")
        self.written.append(qso)
        return len(self.written)


def test_enqueue_then_drain_persists_once() -> None:
    repo = FlakyRepo()
    qlog = QsoLog(repo)
    qlog.enqueue(record())
    assert qlog.pending == 1
    run(qlog.drain_once())
    assert qlog.pending == 0
    assert repo.written == [record()]


def test_failed_write_retries_without_loss() -> None:
    repo = FlakyRepo(fail_first=2)
    qlog = QsoLog(repo)
    qlog.enqueue(record())
    run(qlog.drain_once())  # attempt 1 fails
    run(qlog.drain_once())  # attempt 2 fails
    assert qlog.pending == 1
    run(qlog.drain_once())  # attempt 3 succeeds
    assert qlog.pending == 0
    assert len(repo.written) == 1


def test_exhausted_attempts_spill_to_journal(tmp_path) -> None:
    repo = FlakyRepo(fail_first=999)
    qlog = QsoLog(repo, pending_path=str(tmp_path / "pending.jsonl"), max_attempts=3)
    qlog.enqueue(record())
    for _ in range(3):
        run(qlog.drain_once())
    assert qlog.pending == 0
    assert tmp_path.joinpath("pending.jsonl").exists()
    assert "K1ABC" in tmp_path.joinpath("pending.jsonl").read_text()


def test_recover_reads_journal_into_queue(tmp_path) -> None:
    repo = FlakyRepo(fail_first=999)
    qlog = QsoLog(repo, pending_path=str(tmp_path / "pending.jsonl"), max_attempts=2)
    qlog.enqueue(record())
    run(qlog.drain_once())
    run(qlog.drain_once())
    assert qlog.pending == 0  # spilled

    fresh = QsoLog(Repository(":memory:"), pending_path=str(tmp_path / "pending.jsonl"))
    assert fresh.recover() == 1
    assert fresh.pending == 1
    run(fresh.drain_once())
    assert fresh.pending == 0
    assert fresh.repository.list_qsos()[0].dx_call == "K1ABC"
    # Journal cleared so a second restart cannot duplicate.
    assert tmp_path.joinpath("pending.jsonl").read_text() == ""


def test_flush_persists_remaining_queue(tmp_path) -> None:
    repo = Repository(":memory:")
    qlog = QsoLog(repo, pending_path=str(tmp_path / "pending.jsonl"))
    qlog.enqueue(record())
    run(qlog.flush())
    assert qlog.pending == 0
    assert len(repo.list_qsos()) == 1


def test_recover_skips_malformed_lines(tmp_path) -> None:
    journal = tmp_path / "pending.jsonl"
    journal.write_text('{"dx_call":"K1ABC","my_call":"M0XX","my_grid":"IO91"}\nnot json\n')
    qlog = QsoLog(Repository(":memory:"), pending_path=str(journal))
    assert qlog.recover() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/engine/test_qso_log.py -q`
Expected: FAIL with `ImportError: cannot import name 'QsoLog'` (and the old `record_qso` import no longer resolves).

- [ ] **Step 3: Implement `QsoLog`**

Rewrite `server/engine/qso_log.py`:

```python
"""Sequencer log record → canonical QSO store, durably (§7.5, UC-005).

The sequencer fires ``on_qso`` the moment a QSO completes; :class:`QsoLog`
owns the record from then on — out of reach of ``reply_to``/``start_cq``
resets.  The composition watchdog drains at most one record per tick;
persistent failures spill to a dead-letter file so a restart can retry
them.  Enqueue/drain/flush run on the event loop thread; the only blocking
call (``repository.record_qso``) is offloaded with ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .repository import Repository
from .sequencer import QSORecord

log = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_PENDING_PATH = "data/qso-pending.jsonl"

_RECORD_FIELDS = tuple(QSORecord.__dataclass_fields__)


@dataclass
class QsoLog:
    """Durable completed-QSO queue drained to the canonical store."""

    repository: Repository
    pending_path: str = DEFAULT_PENDING_PATH
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    clock: Callable[[], float] = time.time
    _queue: deque[tuple[QSORecord, int]] = field(
        default_factory=deque, repr=False
    )

    @property
    def pending(self) -> int:
        """Records not yet persisted (health snapshot)."""

        return len(self._queue)

    def enqueue(self, record: QSORecord) -> None:
        """Accept one completed QSO from the sequencer's ``on_qso``."""

        self._queue.append((record, 0))

    async def drain_once(self) -> None:
        """Persist at most one record; retry, then spill after max attempts."""

        if not self._queue:
            return
        record, attempts = self._queue[0]
        try:
            await asyncio.to_thread(self.repository.record_qso, record)
        except Exception:
            attempts += 1
            if attempts >= self.max_attempts:
                self._queue.popleft()
                self._journal(record, attempts)
                log.error(
                    "QSO %s to %s spilled to dead-letter after %d attempts",
                    record.my_call,
                    record.dx_call,
                    attempts,
                )
            else:
                self._queue[0] = (record, attempts)
        else:
            self._queue.popleft()

    def recover(self) -> int:
        """Re-queue records journaled by an earlier failed run; clears the file."""

        records = self._read_journal()
        if records:
            log.warning(
                "recovering %d QSO record(s) from dead-letter journal %s",
                len(records),
                self.pending_path,
            )
        self._clear_journal()
        for recovered in records:
            self._queue.append((recovered, 0))
        return len(records)

    async def flush(self) -> None:
        """Best-effort persist of every queued and journaled record."""

        while self._queue:
            record, attempts = self._queue.popleft()
            try:
                await asyncio.to_thread(self.repository.record_qso, record)
            except Exception:
                self._journal(record, attempts)
        for recovered in self._read_journal():
            try:
                await asyncio.to_thread(self.repository.record_qso, recovered)
            except Exception:
                log.warning(
                    "shutdown flush could not persist QSO to %s", recovered.dx_call
                )
        self._clear_journal()

    # ---- internals -----------------------------------------------------

    def _journal(self, record: QSORecord, attempts: int) -> None:
        payload = {key: getattr(record, key) for key in _RECORD_FIELDS}
        payload["attempts"] = attempts
        payload["journaled_epoch"] = self.clock()
        try:
            with open(self.pending_path, "a") as stream:
                stream.write(json.dumps(payload) + "\n")
        except OSError:
            log.exception("could not write dead-letter journal %s", self.pending_path)

    def _read_journal(self) -> list[QSORecord]:
        try:
            with open(self.pending_path) as stream:
                lines = [line for line in stream if line.strip()]
        except FileNotFoundError:
            return []
        records: list[QSORecord] = []
        for line in lines:
            try:
                data = json.loads(line)
                records.append(
                    QSORecord(
                        **{key: data[key] for key in _RECORD_FIELDS if key in data}
                    )
                )
            except (ValueError, TypeError):
                log.warning("skipping malformed dead-letter line: %s", line.strip()[:80])
        return records

    def _clear_journal(self) -> None:
        try:
            with open(self.pending_path, "w"):
                pass
        except OSError:
            log.exception("could not clear dead-letter journal %s", self.pending_path)
```

Note: `_journal` runs on the event loop thread (rare, small append) — acceptable without `to_thread`; the plan's global constraint names `record_qso` as the only blocking call to offload.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/engine/test_qso_log.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Run the full engine suite to confirm no stale `record_qso`/`pop_log_record` references**

Run: `venv/bin/python -m pytest tests/engine -q`
Expected: PASS. (If any other test still imports `record_qso` or calls `pop_log_record`, fix it — grep `git grep -n "pop_log_record\|record_qso"` and update only the tests, then re-run.)

- [ ] **Step 6: Commit**

```bash
git add server/engine/qso_log.py tests/engine/test_qso_log.py
git commit -m "feat(log): durable QsoLog queue with retry, dead-letter journal, recover and flush"
```

---

### Task 4: Wire `QsoLog` into the composition root

**Files:**
- Modify: `server/web/api.py`, `server/main.py`
- Test: `tests/web/test_api.py`

**Interfaces:**
- Consumes: `QsoLog` (Task 3), `band_from_freq_hz` (Task 1), `QsoContext` (Task 2).
- Produces: `AppState.qso_log: QsoLog | None`; health snapshot `qso_pending: int`; main.py watchdog calls `await state.qso_log.drain_once()`, startup `state.qso_log.recover()`, shutdown `await asyncio.to_thread(state.qso_log.flush)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/web/test_api.py`:

```python
def test_health_reports_pending_qso_log(state: AppState, client: TestClient) -> None:
    from server.engine.qso_log import QsoLog

    state.qso_log = QsoLog(state.repository)
    state.qso_log.enqueue(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="K1ABC")
    )
    session_id = login(client)
    response = client.get(
        "/api/v1/diagnostics/health", headers=auth_headers(session_id)
    )
    assert response.status_code == 200
    assert response.json()["qso_pending"] == 1
    state.qso_log = None  # keep other tests in this module deterministic
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/web/test_api.py::test_health_reports_pending_qso_log -v`
Expected: FAIL with `KeyError: 'qso_pending'` (field absent from health).

- [ ] **Step 3: Implement the API change**

In `server/web/api.py`:

1. Add a `qso_log: Any = None` field to the `AppState` dataclass (near `cq_loop`):

```python
    cq_loop: Any = None      # wired by a later task
    qso_log: Any = None      # QsoLog durable completed-QSO queue when wired
```

2. In `_health`, add the pending count (guarded so tests that omit `qso_log` still pass):

```python
    if state.qso_log is not None:
        health["qso_pending"] = state.qso_log.pending
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/web/test_api.py::test_health_reports_pending_qso_log -v`
Expected: PASS.

- [ ] **Step 5: Implement the main.py wiring**

Edit `server/main.py`:

1. In `create_server`, after `state = AppState(...)` (around line 202), add the `QsoLog` wiring block:

```python
    # Durable QSO logging (§7.5): the sequencer hands each completed record
    # to the queue at completion; the watchdog drains one per tick, and
    # persistent failures spill to a dead-letter journal for recovery.
    from .engine.bands import band_from_freq_hz
    from .engine.qso_log import QsoLog
    from .engine.sequencer import QsoContext

    qso_log = QsoLog(repository, pending_path="data/qso-pending.jsonl")

    def qso_context() -> QsoContext:
        freq = state.radio_freq_hz if state.radio_freq_hz is not None else 0
        return QsoContext(freq_hz=freq, band=band_from_freq_hz(freq))

    sequencer.on_qso = qso_log.enqueue
    sequencer.context = qso_context
    state.qso_log = qso_log
```

2. In the lifespan, right after the `abort_active_qsos` block (line ~453), recover the dead-letter journal:

```python
        state.qso_log.recover()
```

3. Replace the watchdog body (lines ~467-473) with a single drain call:

```python
                    # QSO logging: drain at most one completed record per
                    # tick; the queue retries failures and dead-letters.
                    await state.qso_log.drain_once()
                    if state.cq_loop is not None:
                        state.cq_loop.tick()
```

4. In the lifespan `finally` teardown, before `repository.close()` (line ~525), flush the queue:

```python
            if state.qso_log is not None:
                await asyncio.to_thread(state.qso_log.flush)
            repository.close()
```

(The `record_qso` import at the top of the lifespan and its `from .engine.qso_log import record_qso` line are now unused — remove that import.)

- [ ] **Step 6: Run tests to verify nothing regressed**

Run: `venv/bin/python -m pytest tests/web tests/engine -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server/web/api.py server/main.py tests/web/test_api.py
git commit -m "feat(log): wire QsoLog into composition root (drain/watchdog, recover/startup, flush/shutdown, health pending)"
```

---

### Task 5: ADIF `TIME_ON` from start time + new `TIME_OFF`

**Files:**
- Modify: `server/engine/adif.py`
- Test: `tests/engine/test_adif.py`

**Interfaces:**
- Consumes: `StoredQSO.started_utc` (existing field — now populated for new QSOs by Task 2) and `StoredQSO.completed_epoch`.
- Produces: ADIF `TIME_ON` = `started_utc` when non-empty else completion time; ADIF `TIME_OFF` = completion time.

- [ ] **Step 1: Write the failing tests**

Add to `tests/engine/test_adif.py`:

```python
def test_time_on_uses_started_utc_and_time_off_is_completion() -> None:
    # started_utc 22:13:10, completed epoch 1_700_000_000 = 22:13:20Z
    qso = stored(started_utc="221310")
    doc = generate_adif([qso], generated_epoch=0.0)
    assert "<TIME_ON:6>221310" in doc
    assert "<TIME_OFF:6>221320" in doc


def test_time_on_falls_back_to_completion_for_legacy_rows() -> None:
    qso = stored(started_utc="")  # legacy rows have no start time
    doc = generate_adif([qso], generated_epoch=0.0)
    assert "<TIME_ON:6>221320" in doc
    assert "<TIME_OFF:6>221320" in doc
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/engine/test_adif.py -v`
Expected: FAIL — `TIME_OFF` absent and `TIME_ON` renders from completion regardless of `started_utc`.

- [ ] **Step 3: Implement the change**

Edit `server/engine/adif.py` — in `generate_adif`, replace the `fields += [...]` block (currently lines 67-71) with:

```python
        time_on = qso.started_utc or time.strftime("%H%M%S", when)
        fields += [
            _field("QSO_DATE", time.strftime("%Y%m%d", when)),
            _field("TIME_ON", time_on),
            _field("TIME_OFF", time.strftime("%H%M%S", when)),
            _field("MODE", qso.mode),
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/engine/test_adif.py -q`
Expected: PASS (7 tests — the existing `test_single_record_fields_and_lengths` still passes because `stored()` sets `started_utc="221320"`, matching the completion-minute render).

- [ ] **Step 5: Commit**

```bash
git add server/engine/adif.py tests/engine/test_adif.py
git commit -m "feat(adif): TIME_ON from QSO start with completion fallback; add TIME_OFF"
```

---

### Task 6: Docs — SDD + AGENTS.md + version history

**Files:**
- Modify: `SDD/07-subject-area-model.md`, `SDD/11-component-model.md`, `SDD/14-version-history.md`, `AGENTS.md`

**Interfaces:**
- Consumes: nothing new; documents Tasks 1-5.

- [ ] **Step 1: Update `SDD/07-subject-area-model.md` §7.5**

Append to the §7.5 Persistence Model section:

```markdown
Completed QSOs leave the sequencer at completion and enter a durable `QsoLog`
queue (drained one record per second by the composition watchdog). Persistent
write failures retry, then spill to a `data/qso-pending.jsonl` dead-letter
journal that is re-queued on the next startup and best-effort flushed on
graceful shutdown, so no completed record is lost to a failed write, a
partner reset, or a restart.
```

- [ ] **Step 2: Update `SDD/11-component-model.md` §11.2**

Change the `qso_log.py` row to:

```markdown
| `qso_log.py` | `QsoLog` durable completed-QSO queue: drain-one-per-tick, retry, dead-letter journal, startup recover and shutdown flush |
```

Add a `bands.py` row after it:

```markdown
| `bands.py` | FT8 dial frequency → ADIF band name (server-side mirror of the PWA band selector) |
```

- [ ] **Step 3: Update `SDD/14-version-history.md`**

Add a new version-history entry at the top of the current list (match the existing format; the newest entry goes first). Content:

```markdown
- QSO logging made durable (§7.5, §11.2): the sequencer fires an injected
  `on_qso` callback at completion instead of a destructively-polled
  `pop_log_record`; a new `QsoLog` queue retries failed writes and spills to
  a `data/qso-pending.jsonl` dead-letter journal (startup `recover()`,
  shutdown `flush()`), closing the failed-write, partner-reset and restart
  loss windows. Completed records now capture `started_utc`, `freq_hz` and
  ADIF `band` via injected `clock`/`context`, and ADIF export renders
  `TIME_ON` from the QSO start with a new `TIME_OFF`. New `bands.py` maps FT8
  dial frequencies to ADIF band names. (NFR-072/AD-014 unchanged.)
```

- [ ] **Step 4: Update `AGENTS.md` module table**

In the `server/engine/` row (line 43), append `QsoLog 队列`/`bands` to the engine responsibilities list — e.g. change the tail `qso_log 落库助手` to `qso_log（QsoLog 持久化队列：重试 + dead-letter + recover/flush）`, and add `bands（FT8 频率→ADIF 波段）` before `、ADIF`.

- [ ] **Step 5: Run the full suite and commit**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: PASS (full suite).

```bash
git add SDD/07-subject-area-model.md SDD/11-component-model.md SDD/14-version-history.md AGENTS.md
git commit -m "docs(sdd): durable QsoLog queue, freq/band metadata and ADIF TIME_ON/TIME_OFF"
```

---

## Acceptance Criteria (from the spec)

- [ ] A completed QSO followed immediately by `reply_to()`/`start_cq()`/restart still lands in SQLite (Task 2 + Task 3 regression tests).
- [ ] A failed write retries and ultimately dead-letters; `recover()` on a fresh process re-queues it (Task 3).
- [ ] New QSOs export ADIF with `FREQ`/`BAND`/`TIME_ON`(start)/`TIME_OFF`(completion) (Tasks 1, 2, 5).
- [ ] The 9 pre-existing `freq_hz=0` rows still export without `FREQ`/`BAND` (Task 5 fallback).
- [ ] Full pytest suite green (Task 6).
