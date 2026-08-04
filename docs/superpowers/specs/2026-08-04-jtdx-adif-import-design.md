# MRRC-FT8 JTDX ADIF Import + LOG Recent-Week Window — Design

**Date:** 2026-08-04
**Status:** Draft for review
**Scope:** Server-side automatic sync of the JTDX `wsjtx_log.adi` history into the canonical QSO store, plus a 7-day window on every LOG surface (list + ADIF export) to keep ~10k-record history from overloading the cockpit.

## 1. Purpose

The operator has ~10,263 completed QSOs (2023-02-27 → 2026-08-02, all `BG1SB`, 10,257 FT8 + 6 MFSK/FT4, bands 2m–80m) in `~/FB/JTDX/wsjtx_log.adi`. The canonical store (`mrrc-ft8.db`) only holds 23 live QSOs. The operator wants the JTDX history synced into the canonical store automatically, and the LOG page restricted to the last week so 10k rows never overload the browser.

Confirmed decisions (brainstorm, 2026-08-04):
- Import form: **C — server-side automatic sync** (no web upload, no one-shot CLI).
- Sync timing: **A — sync once at startup, then hourly background incremental checks**; tolerant of JTDX writing the file mid-QSO (half-written trailing line skipped).
- LOG window: **B — backend filters to the last 7 days on both `/logs/qsos` and `/logs/adif`**; ADIF export is windowed too (backup semantics accepted by the operator).
- Record provenance: **A — `source` column** (`jtdx` / `live`) + automatic dedupe key; UI list does not distinguish source.
- Path: configured via `.env` `MRRC_FT8_JTDX_LOG_PATH` (empty = feature disabled).

## 2. Current Structure

- `server/engine/repository.py` — `Repository` (SQLite, one lock, `_write` DBMOVED self-heal). `qso` table: `id, my_call, my_grid, dx_call, dx_grid, report_sent, report_rcvd, started_utc, mode, freq_hz, band, status, completed_epoch, void_actor, void_reason`. `user_version = 1`. `record_qso()` inserts a live QSO; `list_qsos(include_void=True)` returns all newest-first; `worked_calls()` returns base calls of non-void rows (drives "hide already-worked").
- `server/main.py` — lifespan composition root; `lease_watchdog()` (1 s) and `maintenance()` (3600 s) background tasks; `ServerConfig.from_env()` loads `.env` via `load_dotenv`.
- `server/web/api.py` — `GET /logs/qsos` → `_ok({qsos, revision})` (envelope fix landed 2026-08-04); `GET /logs/adif` → `PlainTextResponse` of `generate_adif(qsos)`.
- `server/web/static/js/settings.js` — `openLogView()` renders `api.qsos()` rows + count into the full-screen overlay.
- `server/engine/adif.py` — ADIF **export** generator (from canonical store). No import parser exists.

## 3. Design

### 3.1 Schema migration v1 → v2 (repository.py)

- Add column: `ALTER TABLE qso ADD COLUMN source TEXT NOT NULL DEFAULT 'live'` — existing 23 rows become `'live'` automatically.
- `SCHEMA_VERSION` 1 → 2; `_SCHEMA` CREATE TABLE gains `source TEXT NOT NULL DEFAULT 'live'`; `_migrate_locked` gains a `version < 2` branch running the ALTER (kept separate so fresh CREATE TABLE + ALTER paths both converge on v2).
- `_QSO_COLUMNS`, `StoredQSO` gain `source`; `_to_stored` reads it.

### 3.2 ADIF import module `server/engine/adif_import.py` (new)

Pure stdlib, hardware-free, testable.

- `parse_adif(text) -> list[dict]` — tolerant parser:
  - Skip the `WSJT-X ADIF Export` header / `<eoh>` line.
  - Skip any trailing line without `<eor>` (JTDX may be mid-write; a QSO is only accepted once its `<eor>` is present).
  - Skip malformed lines (bad `<tag:n>` shapes); never raise on one bad record.
  - Fields extracted with a regex over `<tag:len>value` (len advisory, not enforced).
- `record_to_qsorecord(fields, my_call, my_grid) -> QSORecord | None`:
  - `dx_call` from `call`; `dx_grid` from `gridsquare` (empty → `""`).
  - `report_sent`/`report_rcvd`: parse `+00/-15` strings → int; missing → `None`.
  - `started_utc` = `time_on` (HHMMSS) verbatim; `mode` verbatim (`MFSK` stays `MFSK`).
  - `freq_hz` = round(float(`freq` MHz) × 1e6).
  - `band` verbatim.
  - `my_grid` = uppercase `my_gridsquare` (the file mixes `ON80da` / `ON80` / `ON80DA`).
  - `completed_epoch` = UTC epoch of `qso_date_off`+`time_off`; falls back to `qso_date`+`time_on` when off-fields are absent.
  - Skip records whose `qso_date`/`time_on`/`call` are missing.
- `dedupe_key(record) -> tuple` — `(dx_call, started_utc, band, freq_hz)`; same second/call/band/freq cannot physically complete two QSOs. Validated in tests against the real file's 31 `(qso_date, time_on, call, band)` duplicates (freq must disambiguate; if not, key grows `completed_epoch`).
- `sync_jtdx_log(repository, path, *, my_call, my_grid, clock) -> SyncReport`:
  1. Read file; missing file → warning report, no crash.
  2. Parse + map → candidate `QSORecord`s.
  3. Load existing `source='jtdx'` key set from the store; also skip keys already present in the incoming batch (in-batch dedupe).
  4. Insert new records via `repository.import_qsos(records)` in one transaction (audit-trailed).
  5. Return `SyncReport(parsed, inserted, skipped, error)`.

### 3.3 Repository additions

- `import_qsos(records, *, source="jtdx") -> int` — bulk insert in one transaction (`INSERT INTO qso ...` per record inside `with self._db:`), records `qso_event` (`jtdx_import`), returns count. Reuses `_write` so DBMOVED self-heal applies.
- `jtdx_keys() -> set[tuple]` — existing `source='jtdx'` dedupe keys (thread-safe, one SELECT).
- `list_qsos(*, include_void=True, since_days: float | None = None)` — `since_days` adds `WHERE completed_epoch >= ?` (now − days); `None` = no window (kept for `worked_calls`-style consumers and tests).
- `record_qso` keeps default `source="live"` (new signature param).

### 3.4 Sync scheduling (main.py)

- `ServerConfig` gains `jtdx_log_path: str | None` from `MRRC_FT8_JTDX_LOG_PATH` (empty → `None`).
- `create_server()` builds `SyncScheduler(repository, path, my_call, my_grid)` (or plain closures — no new class unless two call sites need it).
- Lifespan: after `safety.start()` and capture/worker startup, run an initial sync via `asyncio.to_thread` (non-blocking; failure logged, never faults the safety controller). Then a `jtdx_sync()` background task every 3600 s (mirrors `maintenance()`), first run delayed by the initial sync so startup doesn't double-sync.
- Each sync result logged (`inserted N, skipped M`) and one `audit_event` row written (`jtdx_import`, detail `inserted=N skipped=M`).
- Disabled (`path is None`): skip task entirely, log once at INFO.

### 3.5 Recent-week window (web/api.py + repository)

- `GET /logs/qsos` → `repository.list_qsos(since_days=7)`; response unchanged shape (`_ok({qsos, revision})`), `qsos` now ≤ 7 days.
- `GET /logs/adif` → `list_qsos(include_void=False, since_days=7)` — windowed export (decision B).
- Frontend `settings.js` unchanged apart from an optional header hint "last 7 days"; the count badge already reflects the returned rows.

### 3.6 Non-goals

- No web upload UI, no one-shot CLI, no file-watch (inotify) sync, no pagination/load-more.
- No per-source filtering in the LOG UI.
- No deletion/sync-down of the canonical store when the JTDX file shrinks (import is additive only).

## 4. Data Flow

Startup (or hourly tick) → read `wsjtx_log.adi` (tolerate half-line) → parse/map to `QSORecord`s → subtract existing `jtdx` keys (in-file + in-batch) → bulk insert `source='jtdx'` → audit + INFO log. LOG overlay → `GET /logs/qsos` → `list_qsos(since_days=7)` → rows + count (≤ 7 days). ADIF link → windowed export. `worked_calls` continues to read all rows (hide-already-worked covers full history).

## 5. Error Handling

- Missing/unreadable file → warning report, no crash, next hourly tick retries.
- Malformed/half-written record → skipped, counted; sync continues.
- DB write failure → `_write` DBMOVED self-heal retry; persistent failure logged via `log.exception`, sync skipped this tick, next tick retries. Never raises into the lifespan task loop (try/except around the whole sync).
- Import never touches PTT/rig/audio; no interaction with the safety controller.

## 6. Testing

- `tests/engine/test_adif_import.py` (new):
  - Parser: normal record; half-written trailing line skipped; malformed tag line skipped; case-insensitive tags; `my_gridsquare` normalization; `freq` MHz→Hz; missing gridsquare → `""`; missing off-fields → `time_on` epoch fallback.
  - Dedupe key: real-file fixture asserts the 31 `(qso_date, time_on, call, band)` duplicates get distinct keys (freq disambiguation); if the fixture proves freq insufficient, key adds `completed_epoch` and the test documents it.
  - Sync: first run inserts N; second run (same file) inserts 0; appended records insert only the delta; audit row written.
- `tests/engine/test_repository.py`: v1→v2 migration preserves rows + `source='live'` default; `import_qsos` sets `source='jtdx'`; `list_qsos(since_days=7)` excludes old rows; `since_days=None` returns all.
- `tests/web/test_api.py`: `/logs/qsos` with one 8-day-old row and one fresh row returns only the fresh; `/logs/adif` similarly windowed.
- `tests/web/test_main.py`: config with empty `MRRC_FT8_JTDX_LOG_PATH` disables sync (no crash); fixture file triggers one import at startup.

## 7. Deployment / Docs

- `.env` example: `MRRC_FT8_JTDX_LOG_PATH=~/FB/JTDX/wsjtx_log.adi`.
- `AGENTS.md` module table: add `adif_import.py`.
- SDD: `05` (new NFR: LOG window, import provenance), `07` (subject model: source on QSO), `11` (component table), `12` (operational: .env var, hourly job), `14` (version history).
