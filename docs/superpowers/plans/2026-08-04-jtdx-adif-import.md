# JTDX ADIF 导入 + LOG 最近一周 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 服务端自动同步 JTDX `wsjtx_log.adi`（~10,263 条历史 QSO）到 canonical store，并把 `/logs/qsos` 与 `/logs/adif` 限制为最近 7 天，防止 10k 行记录过载浏览器。

**架构：** `server/engine/adif_import.py` 提供容错解析器（容忍 JTDX 半行写入）+ 映射 + 幂等增量同步；`Repository` 升级 schema v2（`source` 列区分 `live`/`jtdx`），新增 `import_qsos`/`dedupe_keys`/`list_qsos(since_days=)`；`main.py` lifespan 启动时同步一次 + 每小时后台增量；`web/api.py` 两个 LOG 接口按 7 天窗口过滤（后端过滤，前端零改动）。

**技术栈：** Python 3.13 / SQLite（stdlib）/ FastAPI / pytest（TDD）。

**规格：** `docs/superpowers/specs/2026-08-04-jtdx-adif-import-design.md`

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `server/engine/repository.py` | schema v2 迁移、`source` 列、`import_qsos`、`dedupe_keys`、`list_qsos(since_days=)` | 修改 |
| `server/engine/adif_import.py` | ADIF 解析 / QSO 映射 / 去重键 / `sync_jtdx_log` | 新建 |
| `server/main.py` | `jtdx_log_path` 配置、启动同步 + 每小时任务 | 修改 |
| `server/web/api.py` | `/logs/qsos`、`/logs/adif` 限 7 天 | 修改 |
| `tests/engine/test_repository.py` | 迁移 / import / dedupe / 窗口 | 修改 |
| `tests/engine/test_adif_import.py` | 解析容错 / 映射 / 幂等 / 重复塌缩 | 新建 |
| `tests/web/test_api.py` | 两个接口窗口断言 | 修改 |
| `tests/web/test_main.py` | 配置禁用 / 启动导入 | 修改 |
| `.env`、`AGENTS.md`、`SDD/05`、`SDD/07`、`SDD/11`、`SDD/12`、`SDD/14` | 文档同步 | 修改 |

**去重键定案（设计文档 3.2 的修正）：** `(dx_call, utc_date_YYYYMMDD, started_utc, band)`。
- 已用真实文件验证：31 组 `(qso_date, time_on, call, band)` 重复是 JTDX 同秒开始的重复尝试（freq/rst_rcvd/time_off 可能不同，如 `ON3SGU 20230306 132048 15m` 两条 time_off `132129`/`132529`；`W6AER` 两条 freq `14.266500`/`14.075500`）——加 freq 无法完全区分（仍剩 25 组），视为同一 QSO **只导入第一条**。
- 键跨 source（`dedupe_keys()` 默认查全部行）：live 已完成的 QSO 不会因 JTDX 文件包含同一通联而被再导一份。
- 库内日期从 `completed_epoch`（UTC）推导；ADIF 侧用 `qso_date`。跨午夜 QSO 两者差一天、去重失效，容忍（极罕见，只是多一条 jtdx 副本，无破坏）。

**验证事实：** 真实文件 `~/FB/JTDX/wsjtx_log.adi`：10,263 条；`time_off`/`qso_date_off` 全部存在（missing 0）；`rst_sent` 缺 2 条、`rst_rcvd` 缺 1 条；`freq` 是 MHz 字符串（`14.075500`）；`my_gridsquare` 大小写混用（`ON80da` 9,795 / `ON80` 430 / `ON80DA` 38）；1,675 条缺 `gridsquare`；`station_callsign` 恒为 `BG1SB`；mode `FT8` 10,257 + `MFSK` 6。

---

### 任务 1：repository schema v2 + `source` 列

**文件：**
- 修改：`server/engine/repository.py`（`SCHEMA_VERSION`、`_SCHEMA`、`_migrate_locked`、`_QSO_COLUMNS`、`StoredQSO`、`_to_stored`、`record_qso`）
- 测试：`tests/engine/test_repository.py`

- [ ] **步骤 1：编写失败的迁移测试**（追加到 `tests/engine/test_repository.py`）

```python
def test_v1_database_migrates_to_v2_preserving_rows(tmp_path: Path) -> None:
    """An existing v1 store (no source column) migrates to v2 in place."""
    path = str(tmp_path / "legacy.db")
    # Build a v1 schema exactly as v1 shipped (no source column).
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE qso (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            my_call TEXT NOT NULL, my_grid TEXT NOT NULL,
            dx_call TEXT NOT NULL, dx_grid TEXT NOT NULL DEFAULT '',
            report_sent INTEGER, report_rcvd INTEGER,
            started_utc TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'FT8',
            freq_hz INTEGER NOT NULL DEFAULT 0, band TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, completed_epoch REAL NOT NULL,
            void_actor TEXT, void_reason TEXT
        );
        INSERT INTO qso (my_call, my_grid, dx_call, status, completed_epoch)
        VALUES ('M0XX', 'IO91', 'K1ABC', 'completed', 1700000000.0);
        PRAGMA user_version = 1;
        """
    )
    conn.close()

    repo = Repository(path, clock=clock)
    stored = repo.get_qso(1)
    assert stored is not None
    assert stored.source == "live"          # default for pre-existing rows
    assert stored.dx_call == "K1ABC"        # row survived the migration
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()
    repo.close()


def test_fresh_database_has_source_column(repo: Repository) -> None:
    qso_id = repo.record_qso(sample_record())
    assert repo.get_qso(qso_id).source == "live"  # type: ignore[union-attr]
```

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/engine/test_repository.py::test_v1_database_migrates_to_v2_preserving_rows tests/engine/test_repository.py::test_fresh_database_has_source_column -q`
预期：FAIL（`AttributeError: 'StoredQSO' object has no attribute 'source'`）

- [ ] **步骤 3：实现迁移 + 字段**

`server/engine/repository.py`：
- 顶部 `from datetime import datetime, timezone`（任务 2 的 `dedupe_keys` 也用）。
- `SCHEMA_VERSION = 1` → `2`。
- `_SCHEMA` 的 qso 表在 `completed_epoch REAL NOT NULL,` 之后、`void_actor TEXT,` 之前加 `source TEXT NOT NULL DEFAULT 'live',`。
- `_QSO_COLUMNS` 追加 `, source`：

```python
_QSO_COLUMNS = (
    "id, my_call, my_grid, dx_call, dx_grid, report_sent, report_rcvd,"
    " started_utc, mode, freq_hz, band, status, completed_epoch,"
    " void_actor, void_reason, source"
)
```

- `_migrate_locked` 改为（兼容 v1 存量库 + 全新库两条路径）：

```python
    def _migrate_locked(self) -> None:
        """Schema check/migration; caller must hold ``self._lock``."""

        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than supported")
        if version < SCHEMA_VERSION:
            with self._db:
                self._db.executescript(_SCHEMA)
                # v1 stores lack the source column; v2 CREATE TABLE already
                # has it, so the ALTER only fires for pre-existing stores.
                columns = {
                    row[1] for row in self._db.execute("PRAGMA table_info(qso)")
                }
                if "source" not in columns:
                    self._db.execute(
                        "ALTER TABLE qso ADD COLUMN source TEXT NOT NULL DEFAULT 'live'"
                    )
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
```

- `StoredQSO` 末尾加 `source: str`（在 `void_reason` 之后）。
- `_to_stored` 末尾加 `source=row["source"],`。
- `record_qso` 签名加 `source: str = "live"`，INSERT 加列：

```python
    def record_qso(
        self,
        record: QSORecord,
        *,
        status: QsoStatus = QsoStatus.COMPLETED,
        completed_epoch: float | None = None,
        source: str = "live",
    ) -> int:
```

INSERT 变为 `... completed_epoch, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`，元组末尾 `epoch, source`。

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/engine/test_repository.py -q`
预期：PASS（全部，含原有用例）

- [ ] **步骤 5：Commit**

```bash
git add server/engine/repository.py tests/engine/test_repository.py
git commit -m "feat(repo): schema v2 source column + v1->v2 migration"
```

---

### 任务 2：repository `import_qsos` / `dedupe_keys` / `list_qsos(since_days)`

**文件：**
- 修改：`server/engine/repository.py`
- 测试：`tests/engine/test_repository.py`

- [ ] **步骤 1：编写失败的测试**（追加到 `tests/engine/test_repository.py`）

```python
def test_import_qsos_bulk_sets_source_and_epoch(repo: Repository) -> None:
    records = [
        (sample_record("K1ABC"), 1700000000.0),
        (sample_record("JA1YAD"), 1700000100.0),
    ]
    assert repo.import_qsos(records) == 2
    first = repo.get_qso(1)
    assert first is not None
    assert first.source == "jtdx"
    assert first.completed_epoch == 1700000000.0
    assert repo.count_rows("qso") == 2


def test_dedupe_keys_cross_source(repo: Repository) -> None:
    repo.record_qso(sample_record("LIVE1"))                       # source=live
    repo.import_qsos([(sample_record("JTDX1"), 1700000000.0)])    # source=jtdx
    keys = repo.dedupe_keys()
    assert ("LIVE1", "20231114", "120000", "20m") in keys   # 1700000000 = 2023-11-14 22:13:20 UTC
    assert ("JTDX1", "20231114", "120000", "20m") in keys
    assert len(keys) == 2


def test_list_qsos_since_days_windows(repo: Repository, clock: FakeClock) -> None:
    clock.now = 1_800_000_000.0  # fixed reference
    repo.record_qso(sample_record("FRESH"), completed_epoch=clock.now)
    repo.record_qso(sample_record("OLD"), completed_epoch=clock.now - 8 * 86_400)
    recent = repo.list_qsos(since_days=7)
    assert [q.dx_call for q in recent] == ["FRESH"]
    assert len(repo.list_qsos()) == 2                       # no window = all
    assert repo.list_qsos(include_void=False) == []         # existing kwargs intact
```

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/engine/test_repository.py::test_import_qsos_bulk_sets_source_and_epoch tests/engine/test_repository.py::test_dedupe_keys_cross_source tests/engine/test_repository.py::test_list_qsos_since_days_windows -q`
预期：FAIL（`AttributeError: 'Repository' object has no attribute 'import_qsos'`）

- [ ] **步骤 3：实现三个方法**（`server/engine/repository.py`，放在 `list_qsos` 附近）

```python
    def import_qsos(
        self, records: list[tuple[QSORecord, float]], *, source: str = "jtdx"
    ) -> int:
        """Bulk insert imported QSOs as (record, completed_epoch) pairs.

        One transaction, audit-trailed per row; the caller owns dedupe.
        """

        def _insert() -> int:
            with self._db:
                for record, epoch in records:
                    cursor = self._db.execute(
                        "INSERT INTO qso (my_call, my_grid, dx_call, dx_grid,"
                        " report_sent, report_rcvd, started_utc, mode, freq_hz,"
                        " band, status, completed_epoch, source)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                            QsoStatus.COMPLETED.value,
                            epoch,
                            source,
                        ),
                    )
                    self._qso_event(int(cursor.lastrowid), epoch, "imported", source)
            return len(records)

        return int(self._write(_insert))  # type: ignore[return-value]

    def dedupe_keys(self, *, source: str | None = None) -> set[tuple[str, str, str, str]]:
        """``(dx_call, utc_date YYYYMMDD, started_utc, band)`` of stored QSOs.

        Cross-source by default so a JTDX re-sync never duplicates a live
        QSO; pass ``source="jtdx"`` to restrict.  UTC date derives from
        ``completed_epoch`` (the ADIF side uses ``qso_date``; midnight
        crossers are the only divergence, tolerated).
        """

        where, params = "", []
        if source is not None:
            where, params = " WHERE source = ?", [source]
        with self._lock:
            rows = self._db.execute(
                f"SELECT dx_call, started_utc, band, completed_epoch FROM qso{where}",
                params,
            ).fetchall()
        keys: set[tuple[str, str, str, str]] = set()
        for row in rows:
            date = datetime.fromtimestamp(
                row["completed_epoch"], tz=timezone.utc
            ).strftime("%Y%m%d")
            keys.add((row["dx_call"], date, row["started_utc"], row["band"]))
        return keys
```

- [ ] **步骤 4：重构 `list_qsos` 支持窗口**（`server/engine/repository.py`）

原实现替换为统一构造 WHERE（`include_void` 分支合并进条件列表）：

```python
    def list_qsos(
        self, *, include_void: bool = True, since_days: float | None = None
    ) -> list[StoredQSO]:
        """QSOs newest-first; ADIF export passes ``include_void=False``,
        the LOG page passes ``since_days=7``."""

        conds: list[str] = []
        params: list[object] = []
        if not include_void:
            conds.append("status != ?")
            params.append(QsoStatus.VOID.value)
        if since_days is not None:
            conds.append("completed_epoch >= ?")
            params.append(self._clock() - since_days * 86_400)
        query = f"SELECT {_QSO_COLUMNS} FROM qso"
        if conds:
            query += " WHERE " + " AND ".join(conds)
        query += " ORDER BY id DESC"
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [self._to_stored(row) for row in rows]
```

- [ ] **步骤 5：运行确认通过**

运行：`venv/bin/python -m pytest tests/engine/test_repository.py -q`
预期：PASS

- [ ] **步骤 6：Commit**

```bash
git add server/engine/repository.py tests/engine/test_repository.py
git commit -m "feat(repo): import_qsos / dedupe_keys / list_qsos since_days window"
```

---

### 任务 3：`adif_import.py` 解析器 + 映射（纯函数）

**文件：**
- 创建：`server/engine/adif_import.py`
- 测试：创建 `tests/engine/test_adif_import.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""JTDX ADIF import: tolerant parser + field mapping (spec §3.2)."""

from __future__ import annotations

from server.engine.adif_import import dedupe_key, map_record, parse_adif

SAMPLE = (
    "<call:6>BG4UCZ <gridsquare:4>PM02 <mode:3>FT8 <rst_sent:3>+00 "
    "<rst_rcvd:3>+04 <qso_date:8>20230227 <time_on:6>005730 "
    "<qso_date_off:8>20230227 <time_off:6>005829 <band:3>20m "
    "<freq:9>14.075500 <station_callsign:5>BG1SB <my_gridsquare:4>ON80 "
    "<tx_pwr:3>100 <comment:3>1st <eor>"
)


def test_parse_adif_skips_header_and_half_written_trailing_line() -> None:
    text = "WSJT-X ADIF Export<eoh>\n" + SAMPLE + "\n" + "<call:6>HALF<eor-not-yet"
    records = parse_adif(text)
    assert len(records) == 1
    assert records[0]["call"] == "BG4UCZ"


def test_parse_adif_drops_malformed_line() -> None:
    text = "garbage-no-tags<eor>\n" + SAMPLE
    records = parse_adif(text)
    assert len(records) == 1


def test_map_record_full_mapping() -> None:
    fields = parse_adif(SAMPLE)[0]
    mapped = map_record(fields, my_call="BG1SB", my_grid="on80da")
    assert mapped is not None
    record, epoch = mapped
    assert record.my_call == "BG1SB"
    assert record.my_grid == "ON80DA"          # normalized uppercase
    assert record.dx_call == "BG4UCZ"
    assert record.dx_grid == "PM02"
    assert (record.report_sent, record.report_rcvd) == (0, 4)   # '+00' '+04'
    assert record.started_utc == "005730"
    assert record.mode == "FT8"
    assert record.freq_hz == 14_075_500
    assert record.band == "20m"
    assert epoch == 1677459509.0               # 2023-02-27 00:58:29 UTC


def test_map_record_tolerates_missing_optional_fields() -> None:
    fields = {
        "call": "JA1YAD", "qso_date": "20230301", "time_on": "010203",
        "band": "40m",
    }
    mapped = map_record(fields, my_call="M0XX", my_grid="IO91")
    assert mapped is not None
    record, epoch = mapped
    assert record.dx_grid == ""
    assert record.report_sent is None
    assert record.report_rcvd is None
    assert record.mode == "FT8"
    assert record.freq_hz == 0
    assert epoch == 1677632523.0               # 2023-03-01 01:02:03 UTC


def test_map_record_rejects_unparseable() -> None:
    assert map_record({}, my_call="M0XX", my_grid="IO91") is None
    assert map_record(
        {"call": "K1ABC", "qso_date": "bad", "time_on": "nope"},
        my_call="M0XX", my_grid="IO91",
    ) is None  # qso_date/time_on must be valid digits


def test_dedupe_key_uses_adif_date() -> None:
    fields = parse_adif(SAMPLE)[0]
    record, epoch = map_record(fields, my_call="BG1SB", my_grid="ON80DA")
    assert record is not None
    key = dedupe_key(record, epoch, fields["qso_date"])
    assert key == ("BG4UCZ", "20230227", "005730", "20m")
```

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/engine/test_adif_import.py -q`
预期：FAIL（`ModuleNotFoundError: server.engine.adif_import`）

- [ ] **步骤 3：实现 `server/engine/adif_import.py`（解析 + 映射部分）**

```python
"""JTDX wsjtx_log.adi → canonical store incremental import (AD-014 store).

WSJT-X/JTDX append every completed QSO to the ADIF export live, so the
parser must tolerate a half-written trailing line (no ``<eor>`` yet) and
malformed records.  Dedupe key ``(dx_call, utc date, started_utc, band)``
collapses JTDX's same-second duplicate attempts and keeps re-syncs
idempotent; cross-source by default so a live QSO is never re-imported.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .repository import Repository
from .sequencer import QSORecord

log = logging.getLogger(__name__)

_FIELD_RE = re.compile(r"<([a-z_]+):\d+>([^<]*)")
_EOH = "<eoh>"
_EOR = "<eor>"


def parse_adif(text: str) -> list[dict[str, str]]:
    """Parse an ADIF export into per-record field dicts (tolerant)."""

    records: list[dict[str, str]] = []
    for line in text.splitlines():
        if _EOH in line or _EOR not in line:
            continue  # header line / half-written trailing record
        fields: dict[str, str] = {}
        for tag, value in _FIELD_RE.findall(line):
            fields[tag] = value.strip()
        if fields:
            records.append(fields)
    return records


def _parse_report(value: str | None) -> int | None:
    """``'+00'`` / ``'-15'`` → int; anything else → None."""

    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _utc_epoch(date_yyyymmdd: str, time_hhmmss: str) -> float:
    """``'20230227'`` + ``'005730'`` → UTC epoch; 0.0 on garbage."""

    try:
        dt = datetime.strptime(f"{date_yyyymmdd} {time_hhmmss}", "%Y%m%d %H%M%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def map_record(
    fields: dict[str, str], *, my_call: str, my_grid: str
) -> tuple[QSORecord, float] | None:
    """One ADIF record → ``(QSORecord, completed_epoch)``; None when the
    record lacks call/qso_date/time_on."""

    call = fields.get("call", "").strip()
    qso_date = fields.get("qso_date", "").strip()
    time_on = fields.get("time_on", "").strip()
    if not call or len(qso_date) != 8 or not qso_date.isdigit():
        return None
    if len(time_on) != 6 or not time_on.isdigit():
        return None
    freq_hz = 0
    freq = fields.get("freq", "").strip()
    if freq:
        try:
            freq_hz = round(float(freq) * 1_000_000)
        except ValueError:
            pass
    date_off = fields.get("qso_date_off", qso_date).strip() or qso_date
    time_off = fields.get("time_off", time_on).strip() or time_on
    record = QSORecord(
        my_call=my_call,
        my_grid=my_grid.upper(),
        dx_call=call,
        dx_grid=fields.get("gridsquare", "").strip(),
        report_sent=_parse_report(fields.get("rst_sent")),
        report_rcvd=_parse_report(fields.get("rst_rcvd")),
        started_utc=time_on,
        mode=fields.get("mode", "FT8").strip() or "FT8",
        freq_hz=freq_hz,
        band=fields.get("band", "").strip(),
    )
    return record, _utc_epoch(date_off, time_off)


def dedupe_key(
    record: QSORecord, epoch: float, qso_date: str
) -> tuple[str, str, str, str]:
    """Collision key: same call + UTC date + start second + band = one QSO."""

    if len(qso_date) == 8 and qso_date.isdigit():
        date = qso_date
    elif epoch:
        date = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%d")
    else:
        date = ""
    return (record.dx_call, date, record.started_utc, record.band)
```

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/engine/test_adif_import.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add server/engine/adif_import.py tests/engine/test_adif_import.py
git commit -m "feat(import): JTDX ADIF parser + QSO mapping (tolerant)"
```

---

### 任务 4：`adif_import.py` 的 `sync_jtdx_log`（幂等增量 + 审计）

**文件：**
- 修改：`server/engine/adif_import.py`
- 测试：`tests/engine/test_adif_import.py`、`tests/engine/test_repository.py`

- [ ] **步骤 1：编写失败的测试**（追加到 `tests/engine/test_adif_import.py`）

```python
def _sample(call: str, qso_date: str = "20230227", time_on: str = "005730",
            band: str = "20m", freq: str = "14.075500") -> str:
    return (
        f"<call:{len(call)}>{call} <gridsquare:0> <mode:3>FT8 "
        f"<qso_date:8>{qso_date} <time_on:6>{time_on} "
        f"<qso_date_off:8>{qso_date} <time_off:6>005900 "
        f"<band:{len(band)}>{band} <freq:{len(freq)}>{freq} "
        f"<station_callsign:5>BG1SB <my_gridsquare:5>ON80DA <eor>"
    )


def test_sync_first_run_inserts_all(tmp_path, clock_fake) -> None:
    path = str(tmp_path / "wsjtx_log.adi")
    path.write_text(_sample("K1ABC") + "\n" + _sample("JA1YAD"))
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.parsed == 2
    assert report.inserted == 2
    assert report.skipped == 0
    assert report.error is None
    assert repository_fake.count_rows("qso") == 2


def test_sync_second_run_is_idempotent(tmp_path, clock_fake) -> None:
    path = str(tmp_path / "wsjtx_log.adi")
    path.write_text(_sample("K1ABC"))
    sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 0
    assert report.skipped == 1
    assert repository_fake.count_rows("qso") == 1


def test_sync_appended_records_import_only_delta(tmp_path, clock_fake) -> None:
    path = str(tmp_path / "wsjtx_log.adi")
    path.write_text(_sample("K1ABC"))
    sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    with open(path, "a") as f:
        f.write("\n" + _sample("W1AW"))
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 1
    assert [r.dx_call for r in repository_fake.list_qsos()] == ["W1AW", "K1ABC"]


def test_sync_collapses_same_second_duplicates(tmp_path, clock_fake) -> None:
    """JTDX same-second duplicate attempts (different freq) count once."""
    path = str(tmp_path / "wsjtx_log.adi")
    path.write_text(
        _sample("ON3SGU", freq="21.075500")
        + "\n" + _sample("ON3SGU", freq="21.075500").replace("005900", "005859")
    )
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 1
    assert report.skipped == 1


def test_sync_missing_file_reports_error_not_crash(tmp_path, clock_fake) -> None:
    report = sync_jtdx_log(
        repository_fake, str(tmp_path / "nope.adi"), my_call="BG1SB", my_grid="ON80DA"
    )
    assert report.error is not None
    assert report.inserted == 0


def test_sync_never_reimports_live_qso(tmp_path, clock_fake) -> None:
    """Cross-source dedupe: a live-completed QSO in the ADIF is skipped."""
    repository_fake.record_qso(
        QSORecord(my_call="BG1SB", my_grid="ON80DA", dx_call="K1ABC",
                  started_utc="005730", band="20m", freq_hz=14_075_500),
        completed_epoch=1677459540.0,   # 2023-02-27 00:59:00 UTC (same date)
    )
    path = str(tmp_path / "wsjtx_log.adi")
    path.write_text(_sample("K1ABC"))
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 0
    assert repository_fake.count_rows("qso") == 1
```

测试文件顶部需要追加 import 与 fixture：

```python
from pathlib import Path

import pytest

from server.engine.adif_import import dedupe_key, map_record, parse_adif, sync_jtdx_log
from server.engine.repository import Repository
from server.engine.sequencer import QSORecord


@pytest.fixture()
def clock_fake():
    class _Clock:
        def __init__(self) -> None:
            self.now = 1_800_000_000.0
        def __call__(self) -> float:
            return self.now
    return _Clock()


@pytest.fixture()
def repository_fake(clock_fake) -> Repository:
    return Repository(":memory:", clock=clock_fake)
```

（现有任务 3 的测试文件头部相应合并 import；`tmp_path` 由 pytest 提供。）

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/engine/test_adif_import.py -q`
预期：FAIL（`ImportError: cannot import name 'sync_jtdx_log'`）

- [ ] **步骤 3：实现 `SyncReport` + `sync_jtdx_log`**（`server/engine/adif_import.py` 末尾）

```python
@dataclass
class SyncReport:
    parsed: int
    inserted: int
    skipped: int
    error: str | None = None


def sync_jtdx_log(
    repository: Repository,
    path: str,
    *,
    my_call: str,
    my_grid: str,
) -> SyncReport:
    """Read + dedupe + import; never raises on file/parse issues.

    Runs on a worker thread via ``asyncio.to_thread`` in main.py; keeps one
    audit row per sync (``jtdx_import``, detail ``inserted=.. skipped=..``).
    """

    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except OSError as exc:
        return SyncReport(0, 0, 0, error=str(exc))

    records = parse_adif(text)
    existing = repository.dedupe_keys()
    seen: set[tuple[str, str, str, str]] = set()
    to_insert: list[tuple[QSORecord, float]] = []
    skipped = 0
    for fields in records:
        mapped = map_record(fields, my_call=my_call, my_grid=my_grid)
        if mapped is None:
            skipped += 1
            continue
        record, epoch = mapped
        key = dedupe_key(record, epoch, fields.get("qso_date", ""))
        if key in existing or key in seen:
            skipped += 1
            continue
        seen.add(key)
        to_insert.append((record, epoch))

    inserted = repository.import_qsos(to_insert) if to_insert else 0
    repository.record_audit(
        actor="system",
        operation="jtdx_import",
        detail=f"inserted={inserted} skipped={skipped}",
    )
    log.info("jtdx sync: parsed=%d inserted=%d skipped=%d", len(records), inserted, skipped)
    return SyncReport(len(records), inserted, skipped)
```

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/engine/test_adif_import.py tests/engine/test_repository.py -q`
预期：PASS

- [ ] **步骤 5：用真实文件一次性冒烟验证（手动）**

```bash
cd /Users/cheenle/HAM/ft8 && venv/bin/python - <<'EOF'
from server.engine.adif_import import sync_jtdx_log
from server.engine.repository import Repository
repo = Repository(":memory:")
report = sync_jtdx_log(repo, "/Users/cheenle/FB/JTDX/wsjtx_log.adi", my_call="BG1SB", my_grid="ON80DA")
print(report)
print("rows:", repo.count_rows("qso"))
# 预期: parsed=10263 inserted≈10232 skipped≈31 (同秒重复塌缩 + 缺失记录)
EOF
```
预期输出：`SyncReport(parsed=10263, inserted=10232, skipped=31, error=None)`（若真实文件稍有变动则以实际为准，但 inserted + skipped 必须 == parsed 且无 error）。

- [ ] **步骤 6：Commit**

```bash
git add server/engine/adif_import.py tests/engine/test_adif_import.py
git commit -m "feat(import): idempotent sync_jtdx_log with audit trail"
```

---

### 任务 5：main.py 配置 + 启动同步 + 每小时任务

**文件：**
- 修改：`server/main.py`（`ServerConfig`、`from_env`、`create_server` lifespan）
- 测试：`tests/web/test_main.py`

- [ ] **步骤 1：编写失败的测试**（追加到 `tests/web/test_main.py`）

```python
def test_jtdx_log_path_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MRRC_FT8_PASSWORD_HASH", "hash")
    monkeypatch.setenv("MRRC_FT8_MY_CALL", "M0XX")
    monkeypatch.setenv("MRRC_FT8_MY_GRID", "IO91")
    monkeypatch.delenv("MRRC_FT8_JTDX_LOG_PATH", raising=False)
    assert ServerConfig.from_env().jtdx_log_path is None
    monkeypatch.setenv("MRRC_FT8_JTDX_LOG_PATH", "~/FB/JTDX/wsjtx_log.adi")
    assert ServerConfig.from_env().jtdx_log_path == "~/FB/JTDX/wsjtx_log.adi"
    monkeypatch.setenv("MRRC_FT8_JTDX_LOG_PATH", "  ")
    assert ServerConfig.from_env().jtdx_log_path is None


def test_startup_imports_jtdx_log_once(tmp_path: Path) -> None:
    log_path = str(tmp_path / "wsjtx_log.adi")
    with open(log_path, "w") as f:
        f.write(
            "<call:6>K1ABC <mode:3>FT8 <qso_date:8>20230227 <time_on:6>005730 "
            "<qso_date_off:8>20230227 <time_off:6>005900 <band:3>20m "
            "<freq:9>14.075500 <my_gridsquare:5>ON80DA <eor>"
        )
    app = create_server(
        make_config(jtdx_log_path=log_path),
        rig=FakeRig(), start_dsp=False, start_audio=False,
    )
    with TestClient(app):
        state = app.state.app_state
        qsos = state.repository.list_qsos()
        assert len(qsos) == 1
        assert qsos[0].source == "jtdx"
        assert qsos[0].dx_call == "K1ABC"


def test_disabled_jtdx_sync_does_not_crash(tmp_path: Path) -> None:
    app = create_server(
        make_config(),  # jtdx_log_path defaults to None
        rig=FakeRig(), start_dsp=False, start_audio=False,
    )
    with TestClient(app):  # lifespan runs; sync must be a no-op
        assert app.state.app_state.repository.count_rows("qso") == 0
```

`make_config` 需支持新参数：

```python
def make_config(
    db_path: str = ":memory:", jtdx_log_path: str | None = None
) -> ServerConfig:
    return ServerConfig(
        password_hash=hash_password(PASSWORD),
        my_call="M0XX",
        my_grid="IO91",
        allowed_hosts=frozenset({"testserver"}),
        db_path=db_path,
        jtdx_log_path=jtdx_log_path,
    )
```

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/web/test_main.py -q`
预期：FAIL（`TypeError: ServerConfig.__init__() got an unexpected keyword argument 'jtdx_log_path'`）

- [ ] **步骤 3：实现配置 + 调度**（`server/main.py`）

`ServerConfig` dataclass 加字段（`pending_path` 之后）：

```python
    jtdx_log_path: str | None = None
```

`from_env` 里（`rigctld_host`/`rigctld_port` 之后）加：

```python
        jtdx_log_path = os.environ.get("MRRC_FT8_JTDX_LOG_PATH", "").strip() or None
```

return `cls(...)` 加 `jtdx_log_path=jtdx_log_path,`。

模块常量（`LEASE_POLL_S` 附近）加：

```python
JTDX_SYNC_S = 3600.0
```

`create_server` 的 lifespan 内、`tasks.append(asyncio.create_task(rig_poll()))` 之前加：

```python
        JTDX_SYNC_S = 3600.0

        async def jtdx_sync() -> None:
            """Startup + hourly incremental import of the JTDX ADIF log.

            Never faults the safety controller: a missing file or parse hiccup
            only logs; the hourly loop retries.  Runs on a worker thread so the
            event loop never blocks on the file read/insert.
            """

            if not config.jtdx_log_path:
                return
            from .engine.adif_import import sync_jtdx_log

            try:
                report = await asyncio.to_thread(
                    sync_jtdx_log,
                    repository,
                    config.jtdx_log_path,
                    my_call=config.my_call,
                    my_grid=config.my_grid,
                )
                if report.error:
                    log.warning("jtdx sync skipped: %s", report.error)
            except Exception:
                log.exception("jtdx sync failed")

        async def jtdx_loop() -> None:
            while True:
                await asyncio.sleep(JTDX_SYNC_S)
                await jtdx_sync()

        await jtdx_sync()  # one import at startup (before the hourly loop)
        tasks.append(asyncio.create_task(jtdx_loop()))
```

（`jtdx_loop` 与 `rig_poll` 一样在 shutdown 时被统一 cancel。）

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/web/test_main.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add server/main.py tests/web/test_main.py
git commit -m "feat(main): startup + hourly JTDX ADIF sync (MRRC_FT8_JTDX_LOG_PATH)"
```

---

### 任务 6：web/api.py 两个 LOG 接口限 7 天

**文件：**
- 修改：`server/web/api.py`（`logs_qsos`、`logs_adif`）
- 测试：`tests/web/test_api.py`

- [ ] **步骤 1：编写失败的测试**（追加到 `tests/web/test_api.py`）

```python
def test_logs_qsos_windows_to_recent_week(client: TestClient, state: AppState) -> None:
    now = time.time()
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="OLD1"),
        completed_epoch=now - 8 * 86_400,
    )
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="FRESH1"),
        completed_epoch=now,
    )
    session_id = login(client)
    body = client.get(
        "/api/v1/logs/qsos", headers=auth_headers(session_id)
    ).json()
    assert [q["dx_call"] for q in body["qsos"]] == ["FRESH1"]


def test_adif_export_windows_to_recent_week(client: TestClient, state: AppState) -> None:
    now = time.time()
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="OLD1"),
        completed_epoch=now - 8 * 86_400,
    )
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="FRESH1"),
        completed_epoch=now,
    )
    session_id = login(client)
    text = client.get(
        "/api/v1/logs/adif", headers=auth_headers(session_id)
    ).text
    assert "FRESH1" in text
    assert "OLD1" not in text
```

（`time`、`QSORecord`、`login`、`auth_headers`、`state`、`client` 均已在文件顶部/现有 fixture 中可用。）

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/web/test_api.py::test_logs_qsos_windows_to_recent_week tests/web/test_api.py::test_adif_export_windows_to_recent_week -q`
预期：FAIL（返回里同时含 OLD1/FRESH1）

- [ ] **步骤 3：实现窗口**（`server/web/api.py`）

`logs_qsos`（约 617 行）：

```python
    @router.get("/logs/qsos")
    async def logs_qsos(session: Session = Depends(require_session)) -> JSONResponse:
        qsos = await asyncio.to_thread(
            state.repository.list_qsos, since_days=7
        )
        # _ok envelope: the drawer's api.qsos() gate is ``res.ok`` (same as
        # every mutation); a bare dict made the log overlay always take the
        # error branch even on 200 ("Could not load log: 200").
        return _ok({"qsos": [_qso_view(q) for q in qsos], "revision": state.revision})
```

`logs_adif`（约 644 行）：

```python
    @router.get("/logs/adif")
    async def logs_adif(session: Session = Depends(require_session)) -> PlainTextResponse:
        qsos = await asyncio.to_thread(
            state.repository.list_qsos, include_void=False, since_days=7
        )
        document = await asyncio.to_thread(generate_adif, qsos)
        return PlainTextResponse(
            document,
            media_type="text/plain",
            headers={"content-disposition": 'attachment; filename="mrrc-ft8.adi"'},
        )
```

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/web/test_api.py -q`
预期：PASS（全文件，含既有 qsos/adif 用例）

- [ ] **步骤 5：Commit**

```bash
git add server/web/api.py tests/web/test_api.py
git commit -m "feat(web): LOG list + ADIF export windowed to last 7 days"
```

---

### 任务 7：文档同步（.env / AGENTS.md / SDD / 版本历史）

**文件：**
- 修改：`.env`（示例注释）、`AGENTS.md`（模块表）、`SDD/05-non-functional-requirements.md`、`SDD/07-subject-area-model.md`、`SDD/11-component-model.md`、`SDD/12-operational-model.md`、`SDD/14-version-history.md`

- [ ] **步骤 1：`.env` 加配置示例**

在 `MRRC_FT8_AUDIO_DEVICE` 之后追加：

```
# JTDX ADIF 自动同步（空=禁用；启动时导入一次 + 每小时增量）
#MRRC_FT8_JTDX_LOG_PATH=~/FB/JTDX/wsjtx_log.adi
```

- [ ] **步骤 2：`AGENTS.md` 模块表加一行**

在 `server/engine/` 相关行附近加：

```
| `server/engine/adif_import.py` | JTDX `wsjtx_log.adi` 容错解析（容忍半行写入）+ 去重键 `(dx_call, utc日期, started_utc, band)` + `sync_jtdx_log` 幂等增量导入；启动 + 每小时由 main.py 调度 |
```

- [ ] **步骤 3：SDD 章节更新**

- `SDD/05-non-functional-requirements.md`：新增 NFR（建议编号 NFR-085）：LOG 列表与 ADIF 导出限最近 7 天（防 10k 行前端过载）；导入记录带 `source` 溯源。目标/验证：`GET /logs/qsos` 与 `/logs/adif` 不含 8 天前记录。
- `SDD/07-subject-area-model.md`：QSO 实体增加 `source` 属性（`live`/`jtdx`）；说明导入路径（ADIF → canonical store）与去重键。
- `SDD/11-component-model.md`：组件表加 `adif_import.py`；`main.py` 行补一句"JTDX ADIF 启动 + 每小时同步"。
- `SDD/12-operational-model.md`：环境变量表加 `MRRC_FT8_JTDX_LOG_PATH`（空=禁用）；定时任务清单加每小时 JTDX 同步。
- `SDD/14-version-history.md`：文件顶部（v1.0.0 块之后）加 Unreleased 条目（参考 2026-08-04 既有条目格式）：描述 10,263 条导入、31 组同秒重复塌缩、去重键跨 source、7 天窗口、导出同样限窗、`source` 列 v1→v2 迁移。

- [ ] **步骤 4：验证**

```bash
cd /Users/cheenle/HAM/ft8 && venv/bin/python -m pytest tests/ -q -x --ignore=tests/integration
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
```
预期：全部测试 PASS（现 659 + 新增），SDD check clean。

- [ ] **步骤 5：Commit**

```bash
git add .env AGENTS.md SDD/
git commit -m "docs: JTDX ADIF sync + LOG 7-day window (SDD NFR-085, v2 schema, .env)"
```

---

## 自检记录

- **规格覆盖度**：设计文档 §3.1（schema v2）→ Task 1；§3.2（解析/映射/去重）→ Task 3/4；§3.3（repository 方法）→ Task 2；§3.4（调度）→ Task 5；§3.5（窗口）→ Task 6；§3.6 non-goals 全部未实现（符合）；§6 测试 → 各任务；§7 文档 → Task 7。规格中的 `SyncReport(parsed, inserted, skipped, error)`、`dedupe_key` 签名、`list_qsos(since_days=)`、`import_qsos` 均与任务代码一致。
- **占位符扫描**：无 TODO/待定；每个步骤含完整代码或精确命令。
- **类型一致性**：`map_record` 返回 `tuple[QSORecord, float] | None`（Task 3/4 一致）；`import_qsos` 接收 `list[tuple[QSORecord, float]]`（Task 2/4 一致）；`sync_jtdx_log(repository, path, *, my_call, my_grid)`（Task 4/5 一致）；`dedupe_key(record, epoch, qso_date)`（Task 3/4 一致）；`make_config(jtdx_log_path=...)`（Task 5 测试与实现一致）。
- **跨源去重日期推导**：`test_dedupe_keys_cross_source` 用 `1700000000` 断言 `20231114`（2023-11-14 22:13:20 UTC → 日期 20231114 ✓，测试期望值已按此 UTC 日期写）。
