# RUMLogNG 双向同步程序 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 创建独立的 `rumlog_sync` 包，每 5 分钟（crontab 驱动）在 MRRC-FT8 的 `data/mrrc-ft8.db` 与 RUMLogNG 的 Core Data SQLite（`CoreQsoModel_1.sqlite`）之间双向同步 QSO：拉取 RUMLogNG 新增（只读）→ 合并进 FT8 db（以 RUMLogNG 为准）；推送 FT8 db 新增 → 经 WSJT-X UDP 2237 送入 RUMLogNG。

**架构：** 纯 Python 标准库独立包（不 import `server/`）。单向数据通道：RUMLogNG→FT8 为只读 SQLite 轮询（Z_PK 游标 + WAL 并发读安全）；FT8→RUMLogNG 为 WSJT-X 协议 `HEARTBEAT` + `QSO_LOGGED` UDP 包（0x00 结尾）。幂等靠 qso 表新增列 `rumlog_uuid`（RUMLogNG 侧 UUID）与 `pushed_to_rumlog`（推送状态）；跨库去重 = `dx_call+band` 相同且完成时刻差 ≤120 s。每轮跑完退出，flock 防重入。

**技术栈：** Python 3.11+ 标准库（sqlite3 / socket / json / datetime / fcntl / pathlib）、pytest（已有 dev 依赖）、项目 venv。

**规格：** `docs/superpowers/specs/2026-08-11-rumlogng-sync-design.md`（已批准）

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `rumlog_sync/__init__.py` | 包标识（`__version__`） |
| `rumlog_sync/config.py` | 默认配置 + `data/rumlog-sync.json` 覆盖 + 校验（纯函数） |
| `rumlog_sync/mapper.py` | 纯映射：Core Data 时间戳→unix、RST 解析、行→record dict、record→推送 ADIF 字段、去重匹配谓词 |
| `rumlog_sync/rumlog_reader.py` | `RUMlogReader`：`mode=ro` 打开、列存在性探测、`fetch_since(last_pk)` |
| `rumlog_sync/wsjt_udp.py` | `build_heartbeat` / `build_qso_logged`（WSJT-X UDP 消息构造）、`send_payload`（注入 socket） |
| `rumlog_sync/ft8_db.py` | `Ft8Db`：幂等迁移（加列+state 表）、去重查询、insert/update、推送状态机、sync_state、audit |
| `rumlog_sync/sync.py` | `run_sync_once(cfg)` 一轮编排（拉→合并→推→确认/重推）+ `SyncReport` |
| `rumlog_sync/__main__.py` | CLI：加载配置、flock、跑一轮、退出码；`--smoke` 冒烟旗标 |
| `rumlog_sync/config.example.json` | 配置模板（data/ 已 gitignore，模板入库） |
| `tests/rumlog_sync/conftest.py` | fixtures：模拟 RUMLogNG Core Data db、临时 FT8 db、样本记录 |
| `tests/rumlog_sync/test_config.py` | config 测试 |
| `tests/rumlog_sync/test_mapper.py` | mapper 测试 |
| `tests/rumlog_sync/test_rumlog_reader.py` | reader 测试 |
| `tests/rumlog_sync/test_wsjt_udp.py` | UDP 构造/发送测试（mock socket，不发真包） |
| `tests/rumlog_sync/test_ft8_db.py` | Ft8Db 测试 |
| `tests/rumlog_sync/test_sync.py` | 一轮编排集成测试 |
| `tests/rumlog_sync/test_main_cli.py` | CLI/flock 测试 |

修改文件：`SDD/08-architecture-decisions.md`、`SDD/12-operational-model.md`、`SDD/14-version-history.md`、`AGENTS.md`、`tests/README.md`、`.agents/skills/sdd-guardian/harness/constraints.json`、`.agents/skills/sdd-guardian/harness/index.json`（任务 8）。

---

## 任务 1：config.py

**文件：** 创建 `rumlog_sync/config.py`、`rumlog_sync/__init__.py`；测试 `tests/rumlog_sync/test_config.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""rumlog_sync.config: defaults, JSON override, validation."""

from __future__ import annotations

import json

import pytest

from rumlog_sync.config import DEFAULT_CONFIG, load_config, validate_config

VALID = {
    "ft8_db": "data/mrrc-ft8.db",
    "rumlog_db": "/tmp/CoreQsoModel_1.sqlite",
    "my_call": "BG1SB",
    "my_grid": "ON80DA",
    "udp_host": "127.0.0.1",
    "udp_port": 2237,
    "udp_id": "MRRC-FT8-SYNC",
    "confirm_retries": 3,
    "lock_path": "data/rumlog-sync.lock",
}


def test_defaults_are_self_consistent() -> None:
    cfg = validate_config(dict(DEFAULT_CONFIG))
    assert cfg["udp_port"] == 2237
    assert cfg["confirm_retries"] >= 1


def test_load_config_returns_defaults_when_file_missing(tmp_path) -> None:
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["udp_port"] == 2237
    assert cfg["my_call"] == "BG1SB"


def test_load_config_merges_json_over_defaults(tmp_path) -> None:
    path = tmp_path / "rumlog-sync.json"
    path.write_text(json.dumps({"udp_port": 9999, "my_call": "BA7ABC"}))
    cfg = load_config(path)
    assert cfg["udp_port"] == 9999
    assert cfg["my_call"] == "BA7ABC"
    assert cfg["udp_host"] == "127.0.0.1"  # untouched default


def test_load_config_rejects_bad_json(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_config(path)


def test_validate_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        validate_config({"bogus_key": 1})


def test_validate_config_enforces_types_and_ranges() -> None:
    bad = dict(VALID, udp_port=0)
    with pytest.raises(ValueError):
        validate_config(bad)
    bad2 = dict(VALID, confirm_retries=0)
    with pytest.raises(ValueError):
        validate_config(bad2)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_config.py -v`
预期：FAIL（ModuleNotFoundError: rumlog_sync.config）

- [ ] **步骤 3：实现 config.py 与包标识**

```python
"""Configuration for the RUMLogNG bidirectional sync program."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CONFIG: dict[str, object] = {
    "ft8_db": "data/mrrc-ft8.db",
    "rumlog_db": (
        "/Users/cheenle/Library/Containers/de.dl2rum.RUMlogNG/Data/Library/"
        "Application Support/RUMLogNG/CoreQsoModel_1.sqlite"
    ),
    "my_call": "BG1SB",
    "my_grid": "ON80DA",
    "udp_host": "127.0.0.1",
    "udp_port": 2237,
    "udp_id": "MRRC-FT8-SYNC",
    "confirm_retries": 3,
    "lock_path": "data/rumlog-sync.lock",
}

_VALIDATORS: dict[str, object] = {
    "ft8_db": str,
    "rumlog_db": str,
    "my_call": str,
    "my_grid": str,
    "udp_host": str,
    "udp_port": lambda v: isinstance(v, int) and 1 <= v <= 65535,
    "udp_id": str,
    "confirm_retries": lambda v: isinstance(v, int) and v >= 1,
    "lock_path": str,
}


def validate_config(cfg: dict[str, object]) -> dict[str, object]:
    """Reject unknown keys and out-of-range values; return a safe copy."""

    unknown = set(cfg) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    merged = {**DEFAULT_CONFIG, **cfg}
    for key, check in _VALIDATORS.items():
        value = merged[key]
        if isinstance(check, type):
            if not isinstance(value, check):
                raise ValueError(f"config {key}: expected {check.__name__}, got {value!r}")
        elif not check(value):
            raise ValueError(f"config {key}: invalid value {value!r}")
    return merged


def load_config(path: str | Path) -> dict[str, object]:
    """Load JSON config over defaults; missing file is not an error."""

    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"config {p}: expected JSON object")
    return validate_config(raw)
```

`rumlog_sync/__init__.py`：

```python
"""RUMLogNG ↔ MRRC-FT8 QSO bidirectional sync (spec 2026-08-11)."""

__version__ = "0.1.0"
```

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_config.py -v`
预期：PASS（6 passed）

- [ ] **步骤 5：Commit**

```bash
git add rumlog_sync/ tests/rumlog_sync/
git commit -m "feat(rumlog_sync): config loading with defaults + validation"
```

---

## 任务 2：mapper.py（纯映射与去重）

**文件：** 创建 `rumlog_sync/mapper.py`；测试 `tests/rumlog_sync/test_mapper.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""mapper: Core Data row → FT8 record dict, push ADIF fields, match predicate."""

from __future__ import annotations

from rumlog_sync.mapper import (
    CORE_DATA_EPOCH_OFFSET,
    build_push_adif_fields,
    derive_started_utc,
    map_rumlog_row,
    parse_report,
    same_qso_match,
    to_unix,
)


def test_to_unix_converts_core_data_timestamp() -> None:
    # Core Data timestamps count seconds since 2001-01-01 UTC.
    assert to_unix(978307200) == 978307200 + CORE_DATA_EPOCH_OFFSET


def test_derive_started_utc_formats_hhmmss() -> None:
    assert derive_started_utc(1_677_459_500.0) == "005829"


def test_parse_report_variants() -> None:
    assert parse_report("+00") == 0
    assert parse_report("-15") == -15
    assert parse_report("59") == 59
    assert parse_report("") is None
    assert parse_report("zz") is None


def test_map_rumlog_row_full() -> None:
    row = {
        "Z_PK": 27139,
        "ZCALLSIGN": "TL8GD",
        "ZDATETIME": 1_795_384_049.0 - CORE_DATA_EPOCH_OFFSET,
        "ZQRG": 14.074684,
        "ZBAND": "20m",
        "ZMODE": "FT8",
        "ZLOCATOR": "",
        "ZRSTTX": "-12",
        "ZRSTRX": "-12",
        "ZUUID": bytes.fromhex("C9DE3F473B90413DA8F6049CDFD26A3B"),
    }
    rec = map_rumlog_row(row, my_call="BG1SB", my_grid="ON80DA")
    assert rec["dx_call"] == "TL8GD"
    assert rec["dx_grid"] == ""
    assert rec["mode"] == "FT8"
    assert rec["band"] == "20m"
    assert rec["freq_hz"] == 14_074_684
    assert (rec["report_sent"], rec["report_rcvd"]) == (-12, -12)
    assert rec["rumlog_uuid"] == "C9DE3F473B90413DA8F6049CDFD26A3B"
    assert rec["source"] == "rumlog"
    assert rec["my_call"] == "BG1SB"
    assert rec["my_grid"] == "ON80DA"
    assert rec["completed_epoch"] == 1_795_384_049.0
    assert rec["started_utc"] == derive_started_utc(1_795_384_049.0)


def test_map_rumlog_row_tolerates_garbage_optional_fields() -> None:
    row = {
        "Z_PK": 1,
        "ZCALLSIGN": "JA1YAD",
        "ZDATETIME": 0.0,
        "ZQRG": None,
        "ZBAND": None,
        "ZMODE": None,
        "ZLOCATOR": None,
        "ZRSTTX": None,
        "ZRSTRX": None,
        "ZUUID": None,
    }
    rec = map_rumlog_row(row, my_call="BG1SB", my_grid="ON80DA")
    assert rec["dx_call"] == "JA1YAD"
    assert rec["freq_hz"] == 0
    assert rec["band"] == ""
    assert rec["mode"] == "FT8"  # default
    assert rec["report_sent"] is None
    assert rec["completed_epoch"] == 0.0


def test_build_push_adif_fields_contains_expected_tags() -> None:
    rec = {
        "dx_call": "BG4UCZ",
        "dx_grid": "PM02",
        "mode": "FT8",
        "band": "20m",
        "freq_hz": 14_075_500,
        "report_sent": 0,
        "report_rcvd": 4,
        "started_utc": "005730",
        "completed_epoch": 1_677_459_500.0,
        "my_call": "BG1SB",
    }
    fields = build_push_adif_fields(rec)
    assert fields["CALL"] == "BG4UCZ"
    assert fields["GRIDSQUARE"] == "PM02"
    assert fields["MODE"] == "FT8"
    assert fields["BAND"] == "20m"
    assert fields["FREQ"] == "14.075500"
    assert fields["RST_SENT"] == "+00"
    assert fields["RST_RCVD"] == "+04"
    assert fields["QSO_DATE"] == "20230227"
    assert fields["TIME_ON"] == "005730"
    assert fields["TIME_OFF"] == "005829"
    assert fields["STATION_CALLSIGN"] == "BG1SB"


def test_same_qso_match_window_and_band() -> None:
    assert same_qso_match("TL8GD", "20m", 100.0, 220.0)  # within 120 s
    assert not same_qso_match("TL8GD", "20m", 100.0, 300.0)  # outside
    assert not same_qso_match("TL8GD", "40m", 100.0, 150.0)  # band differs
    assert not same_qso_match("TL8GD", "20m", 100.0, -50.0)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_mapper.py -v`
预期：FAIL（ModuleNotFoundError）

- [ ] **步骤 3：实现 mapper.py**

```python
"""Pure mappings between RUMLogNG Core Data rows and FT8 qso records.

Spec §4/§5: Core Data timestamps count seconds since 2001-01-01 UTC
(offset 978307200 s).  Cross-db dedupe matches dx_call+band with a 120 s
completion-time window.
"""

from __future__ import annotations

from datetime import datetime, timezone

CORE_DATA_EPOCH_OFFSET = 978_307_200
MATCH_WINDOW_S = 120.0
_DEFAULT_MODE = "FT8"


def to_unix(core_data_seconds: float) -> float:
    """Core Data timestamp (seconds since 2001-01-01) → Unix epoch."""

    return float(core_data_seconds) + CORE_DATA_EPOCH_OFFSET


def derive_started_utc(epoch: float) -> str:
    """Unix epoch → ``HHMMSS`` display string (spec §4)."""

    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H%M%S")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_report(value: object) -> int | None:
    """``'+00'`` / ``'-15'`` / ``'59'`` → int; empty/garbage → None."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _safe_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def map_rumlog_row(row: dict[str, object], *, my_call: str, my_grid: str) -> dict[str, object]:
    """One ZCORE_QSO row → FT8 qso record dict (spec §4 table)."""

    epoch = to_unix(float(row.get("ZDATETIME") or 0.0))
    freq_mhz = row.get("ZQRG")
    freq_hz = 0
    if freq_mhz is not None:
        try:
            freq_hz = round(float(freq_mhz) * 1_000_000)
        except (TypeError, ValueError):
            pass
    uuid_raw = row.get("ZUUID")
    uuid_hex = uuid_raw.hex().upper() if isinstance(uuid_raw, bytes) else ""
    return {
        "my_call": my_call,
        "my_grid": my_grid,
        "dx_call": _safe_str(row.get("ZCALLSIGN")),
        "dx_grid": _safe_str(row.get("ZLOCATOR")),
        "report_sent": parse_report(row.get("ZRSTTX")),
        "report_rcvd": parse_report(row.get("ZRSTRX")),
        "started_utc": derive_started_utc(epoch),
        "mode": _safe_str(row.get("ZMODE")) or _DEFAULT_MODE,
        "freq_hz": freq_hz,
        "band": _safe_str(row.get("ZBAND")),
        "status": "completed",
        "completed_epoch": epoch,
        "rumlog_uuid": uuid_hex,
        "source": "rumlog",
    }


def _fmt_report(value: object) -> str:
    return "" if value is None else f"{int(value):+03d}"


def build_push_adif_fields(rec: dict[str, object]) -> dict[str, str]:
    """FT8 qso record → ADIF fields for the WSJT-X QSO_LOGGED payload."""

    epoch = float(rec["completed_epoch"])
    date_utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
    freq_mhz = float(rec.get("freq_hz") or 0) / 1_000_000.0
    return {
        "CALL": _safe_str(rec.get("dx_call")),
        "GRIDSQUARE": _safe_str(rec.get("dx_grid")),
        "MODE": _safe_str(rec.get("mode")) or _DEFAULT_MODE,
        "BAND": _safe_str(rec.get("band")),
        "FREQ": f"{freq_mhz:.6f}",
        "RST_SENT": _fmt_report(rec.get("report_sent")),
        "RST_RCVD": _fmt_report(rec.get("report_rcvd")),
        "QSO_DATE": date_utc.strftime("%Y%m%d"),
        "TIME_ON": _safe_str(rec.get("started_utc")) or date_utc.strftime("%H%M%S"),
        "TIME_OFF": date_utc.strftime("%H%M%S"),
        "STATION_CALLSIGN": _safe_str(rec.get("my_call")),
    }


def same_qso_match(dx_call: str, band: str, epoch_a: float, epoch_b: float) -> bool:
    """True when both sides describe the same QSO (spec §5)."""

    return dx_call != "" and band != "" and abs(epoch_a - epoch_b) <= MATCH_WINDOW_S
```

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_mapper.py -v`
预期：PASS（7 passed）

- [ ] **步骤 5：Commit**

```bash
git add rumlog_sync/mapper.py tests/rumlog_sync/test_mapper.py
git commit -m "feat(rumlog_sync): Core Data ↔ FT8 record mapping + dedupe predicate"
```

---

## 任务 3：rumlog_reader.py（只读 Core Data）

**文件：** 创建 `rumlog_sync/rumlog_reader.py`；测试 `tests/rumlog_sync/test_rumlog_reader.py`；fixtures `tests/rumlog_sync/conftest.py`

- [ ] **步骤 1：编写 fixture 与失败的测试**

`tests/rumlog_sync/conftest.py`：

```python
"""Shared fixtures: simulated RUMLogNG Core Data db + scratch dirs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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


def make_rumlog_db(path: Path, rows: list[dict[str, object]]) -> None:
    """Create a CoreQsoModel_1.sqlite-shaped db; rows use unix-style ZDATETIME.

    Callers pass ``unix_epoch`` values; the fixture converts to Core Data
    seconds so mapper tests stay readable.
    """

    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE ZCORE_QSO ({RUMLOG_COLUMNS})")
    for row in rows:
        data = dict(row)
        if "unix_epoch" in data:
            data["ZDATETIME"] = data.pop("unix_epoch") - CORE_DATA_EPOCH_OFFSET
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        con.execute(
            f"INSERT INTO ZCORE_QSO ({cols}) VALUES ({marks})",
            tuple(data.values()),
        )
    con.commit()
    con.close()


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
```

`tests/rumlog_sync/test_rumlog_reader.py`：

```python
"""rumlog_reader: read-only Core Data access, column probe, Z_PK cursor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rumlog_sync.rumlog_reader import RUMlogReader, SchemaMismatch

from .conftest import make_rumlog_db


def _rows(sample: dict[str, object]) -> list[dict[str, object]]:
    rows = []
    for pk, suffix in ((27137, ""), (27139, "B")):
        row = dict(sample)
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


def test_fetch_since_returns_only_new_rows(tmp_path: Path, sample_rumlog_row) -> None:
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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_rumlog_reader.py -v`
预期：FAIL（ModuleNotFoundError）

- [ ] **步骤 3：实现 rumlog_reader.py**

```python
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
        self._con = sqlite3.connect(
            f"file:{self._db_path}?mode=ro", uri=True, timeout=5.0
        )
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_rumlog_reader.py -v`
预期：PASS（3 passed）

- [ ] **步骤 5：Commit**

```bash
git add rumlog_sync/rumlog_reader.py tests/rumlog_sync/
git commit -m "feat(rumlog_sync): read-only RUMlogNG Core Data reader with schema probe"
```

---

## 任务 4：wsjt_udp.py（WSJT-X UDP 消息）

**文件：** 创建 `rumlog_sync/wsjt_udp.py`；测试 `tests/rumlog_sync/test_wsjt_udp.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""wsjt_udp: WSJT-X UDP message construction + send (never real packets)."""

from __future__ import annotations

from rumlog_sync.wsjt_udp import build_heartbeat, build_qso_logged, send_payload


def test_build_heartbeat_format() -> None:
    payload = build_heartbeat("MRRC-FT8-SYNC", dial_freq_hz=14_074_000)
    text = payload.decode("ascii")
    assert text.startswith("<MessageType:9>HEARTBEAT")
    assert "<Id:14>MRRC-FT8-SYNC" in text
    assert "<DialFrequency:11>14074000" in text
    assert payload.endswith(b"\x00")


def test_build_qso_logged_format() -> None:
    fields = {
        "CALL": "TL8GD",
        "GRIDSQUARE": "",
        "MODE": "FT8",
        "BAND": "20m",
        "FREQ": "14.074684",
        "RST_SENT": "-12",
        "RST_RCVD": "-12",
        "QSO_DATE": "20260810",
        "TIME_ON": "232300",
        "TIME_OFF": "232329",
        "STATION_CALLSIGN": "BG1SB",
    }
    payload = build_qso_logged(fields)
    text = payload.decode("ascii")
    assert text.startswith("<MessageType:10>QSO_LOGGED")
    assert "<ADIF:148>" in text  # length prefix matches record below
    assert "<CALL:5>TL8GD" in text
    assert "<EOR>" in text
    assert payload.endswith(b"\x00")
    # Length prefix must equal the record length between <ADIF:N> and <EOR>.
    record = text.split("<ADIF:", 1)[1].split(">", 1)[1]
    assert len(record) == 148


def test_send_payload_uses_injected_socket() -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeSocket:
        def sendto(self, data: bytes, addr: tuple[str, int]) -> int:
            sent.append((data, addr))
            return len(data)

    send_payload(b"hello\x00", "127.0.0.1", 2237, sock=FakeSocket())
    assert sent == [(b"hello\x00", ("127.0.0.1", 2237))]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_wsjt_udp.py -v`
预期：FAIL（ModuleNotFoundError）

- [ ] **步骤 3：实现 wsjt_udp.py**

```python
"""WSJT-X UDP protocol messages (spec §6).

RUMLogNG listens on UDP 2237 and natively parses WSJT-X messages:
``HEARTBEAT`` registers a client; ``QSO_LOGGED`` carries an ADIF record.
Every message is null-terminated.  ``send_payload`` accepts an injected
socket so tests never touch the network.
"""

from __future__ import annotations

import logging
import socket

log = logging.getLogger(__name__)

_MESSAGE_TYPE_HEARTBEAT = "<MessageType:9>HEARTBEAT"
_MESSAGE_TYPE_QSO_LOGGED = "<MessageType:10>QSO_LOGGED"


def _adif_field(name: str, value: str) -> str:
    return f"<{name}:{len(value)}>{value}"


def build_heartbeat(udp_id: str, *, dial_freq_hz: int = 14_074_000) -> bytes:
    """WSJT-X HEARTBEAT message (spec §6), null-terminated."""

    body = (
        _MESSAGE_TYPE_HEARTBEAT
        + _adif_field("Id", udp_id)
        + _adif_field("DialFrequency", str(dial_freq_hz))
        # Remaining WSJT-X heartbeat fields are zero/empty in our usage.
        + "<ConfigurationName:0><TxMessage:0><TxFreq:0><DeDup:0><SubTxMessage:0>"
        + "<RxDF:0><TxDF:0><TRPeriod:0><ModulationType:0><DXCall:0><DXGrid:0>"
        + "<TxEnabled:0><Transmitting:0><Decoding:0><RxEnabled:0><FECDecoded:0>"
        + "<Watchdog:0><Submode:0><FastMode:0><SpecialOperationMode:0>"
        + "<FrequencyTolerance:0><Tolerance:0><DecoderType:0><Harmonic:0>"
        + _MESSAGE_TYPE_HEARTBEAT
    )
    return (body + "\x00").encode("ascii")


def build_qso_logged(adif_fields: dict[str, str]) -> bytes:
    """WSJT-X QSO_LOGGED message: ``<ADIF:N>record<EOR>`` + null."""

    record = "".join(_adif_field(k, v) for k, v in adif_fields.items() if v != "")
    body = _MESSAGE_TYPE_QSO_LOGGED + _adif_field("ADIF", record) + "<EOR>"
    return (body + "\x00").encode("ascii")


def send_payload(
    payload: bytes,
    host: str,
    port: int,
    *,
    sock: object | None = None,
) -> None:
    """Best-effort UDP send; failures are logged, never raised to the caller."""

    close_sock = sock is None
    client = sock if sock is not None else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client.sendto(payload, (host, port))
    except OSError:
        log.exception("UDP send to %s:%d failed", host, port)
    finally:
        if close_sock:
            client.close()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_wsjt_udp.py -v`
预期：PASS（3 passed）

- [ ] **步骤 5：Commit**

```bash
git add rumlog_sync/wsjt_udp.py tests/rumlog_sync/test_wsjt_udp.py
git commit -m "feat(rumlog_sync): WSJT-X HEARTBEAT/QSO_LOGGED UDP messages"
```

---

## 任务 5：ft8_db.py（FT8 db 访问 + 迁移 + 状态机）

**文件：** 创建 `rumlog_sync/ft8_db.py`；测试 `tests/rumlog_sync/test_ft8_db.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""ft8_db: idempotent migration, dedupe lookup, upsert, push state machine."""

from __future__ import annotations

import sqlite3

import pytest

from rumlog_sync.ft8_db import Ft8Db


def _record(dx_call: str = "TL8GD", epoch: float = 1_795_384_049.0, band: str = "20m"):
    return {
        "my_call": "BG1SB",
        "my_grid": "ON80DA",
        "dx_call": dx_call,
        "dx_grid": "PM02",
        "report_sent": -12,
        "report_rcvd": -12,
        "started_utc": "232300",
        "mode": "FT8",
        "freq_hz": 14_074_684,
        "band": band,
        "status": "completed",
        "completed_epoch": epoch,
        "rumlog_uuid": "",
        "source": "rumlog",
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
    qso_id = db.insert_record(_record())
    assert db.pending_push() == [qso_id]
    db.mark_pushed(qso_id)
    assert db.pending_push() == []
    assert db.tick_unconfirmed(3) == 0  # attempt 1 → 2 (< retries)
    assert db.tick_unconfirmed(3) == 0  # attempt 2 → 3 (< retries)
    assert db.pending_push() == []
    assert db.tick_unconfirmed(3) == 1  # attempt 3 → requeued
    assert db.pending_push() == [qso_id]


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
    assert rows == [("rumlog_pull", "inserted=2 skipped=1")]
```

注意：`_record` 需接受 `rumlog_uuid` 关键字；`get_record` 返回 dict。修正 `_record` 签名：

```python
def _record(dx_call: str = "TL8GD", epoch: float = 1_795_384_049.0,
            band: str = "20m", rumlog_uuid: str = ""):
    return {
        ...,
        "rumlog_uuid": rumlog_uuid,
        "source": "rumlog",
    }
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_ft8_db.py -v`
预期：FAIL（ModuleNotFoundError）

- [ ] **步骤 3：实现 ft8_db.py**

```python
"""FT8 canonical db access for the sync program (spec §8).

The server's Repository keeps its own explicit-column queries, so adding
``rumlog_uuid`` / ``pushed_to_rumlog`` columns is backward compatible.
Migrations are idempotent (PRAGMA probe + ADD COLUMN).  Push state uses a
per-record attempt counter in ``rumlog_sync_state`` (JSON map keyed by qso
id): ``reset_unconfirmed`` requeues records whose unconfirmed pushes have
reached ``confirm_retries``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

STATE_KEY = "rumlog_sync_state"
PUSH_PENDING_KEY = "push_pending"


class Ft8Db:
    """One connection to the FT8 canonical db; caller owns commit/close."""

    def __init__(self, db_path: str | Path) -> None:
        self._con = sqlite3.connect(str(db_path), timeout=10.0)
        self._con.row_factory = sqlite3.Row

    # ---- schema --------------------------------------------------------

    def ensure_schema(self) -> None:
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
            f"CREATE TABLE IF NOT EXISTS {STATE_KEY} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
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
        cur = self._con.execute(
            "INSERT INTO qso (my_call, my_grid, dx_call, dx_grid, report_sent,"
            " report_rcvd, started_utc, mode, freq_hz, band, status,"
            " completed_epoch, rumlog_uuid, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec["my_call"], rec["my_grid"], rec["dx_call"], rec["dx_grid"],
                rec["report_sent"], rec["report_rcvd"], rec["started_utc"],
                rec["mode"], rec["freq_hz"], rec["band"], rec["status"],
                rec["completed_epoch"], rec.get("rumlog_uuid", ""),
                rec.get("source", "rumlog"),
            ),
        )
        return int(cur.lastrowid)

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

    def mark_pushed(self, qso_id: int) -> None:
        self._con.execute(
            "UPDATE qso SET pushed_to_rumlog = 1 WHERE id = ?", (qso_id,)
        )
        pending = json.loads(self.state_get(PUSH_PENDING_KEY) or "{}")
        pending[str(qso_id)] = 1  # fresh unconfirmed push
        self.state_set(PUSH_PENDING_KEY, json.dumps(pending))

    def tick_unconfirmed(self, confirm_retries: int = 3) -> int:
        """Age unconfirmed pushes; requeue past the retry budget.

        Called once per round before new pushes: every still-unconfirmed
        record gains an attempt; one that reaches ``confirm_retries`` is
        requeued (``pushed_to_rumlog=0``) so the next push re-sends it.
        Returns the number requeued.
        """

        pending = json.loads(self.state_get(PUSH_PENDING_KEY) or "{}")
        requeued = 0
        for qso_id_str, attempts in list(pending.items()):
            if int(attempts) >= confirm_retries:
                self._con.execute(
                    "UPDATE qso SET pushed_to_rumlog = 0 WHERE id = ?",
                    (int(qso_id_str),),
                )
                pending.pop(qso_id_str, None)
                requeued += 1
                log.warning(
                    "push for qso %s unconfirmed after %d rounds; requeued",
                    qso_id_str, attempts,
                )
            else:
                pending[qso_id_str] = int(attempts) + 1
        self.state_set(PUSH_PENDING_KEY, json.dumps(pending))
        return requeued

    def confirm_uuid(self, qso_id: int) -> None:
        """Push confirmed (RUMLogNG row observed); drop pending counter."""

        pending = json.loads(self.state_get(PUSH_PENDING_KEY) or "{}")
        pending.pop(str(qso_id), None)
        self.state_set(PUSH_PENDING_KEY, json.dumps(pending))

    def rollback(self) -> None:
        self._con.rollback()

    # ---- sync state ------------------------------------------------------

    def state_get(self, key: str) -> str | None:
        row = self._con.execute(
            f"SELECT value FROM {STATE_KEY} WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def state_set(self, key: str, value: str) -> None:
        self._con.execute(
            f"INSERT INTO {STATE_KEY} (key, value) VALUES (?, ?)"
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_ft8_db.py -v`
预期：PASS（7 passed）

- [ ] **步骤 5：Commit**

```bash
git add rumlog_sync/ft8_db.py tests/rumlog_sync/test_ft8_db.py
git commit -m "feat(rumlog_sync): FT8 db migration, dedupe lookup, push state machine"
```

---

## 任务 6：sync.py（一轮编排）

**文件：** 创建 `rumlog_sync/sync.py`；测试 `tests/rumlog_sync/test_sync.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""sync: one full round — pull, merge, push, confirm, requeue (spec §3.2)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rumlog_sync.ft8_db import Ft8Db
from rumlog_sync.mapper import CORE_DATA_EPOCH_OFFSET, to_unix
from rumlog_sync.rumlog_reader import RUMlogReader
from rumlog_sync.sync import SyncReport, run_sync_once
from rumlog_sync.wsjt_udp import build_heartbeat, build_qso_logged

from .conftest import make_rumlog_db


@dataclass
class CapturedSend:
    payloads: list[bytes]

    def __call__(self, payload: bytes, host: str, port: int, *, sock=None) -> None:
        self.payloads.append(payload)


def _cfg(ft8_path: Path, rumlog_path: Path, retries: int = 3) -> dict[str, object]:
    return {
        "ft8_db": str(ft8_path),
        "rumlog_db": str(rumlog_path),
        "my_call": "BG1SB",
        "my_grid": "ON80DA",
        "udp_host": "127.0.0.1",
        "udp_port": 2237,
        "udp_id": "MRRC-FT8-SYNC",
        "confirm_retries": retries,
        "lock_path": str(ft8_path.parent / "sync.lock"),
    }


def _rumlog_row(callsign: str, pk: int, unix_epoch: float, band: str = "20m"):
    return {
        "ZCALLSIGN": callsign,
        "Z_PK": pk,
        "unix_epoch": unix_epoch,
        "ZQRG": 14.074684,
        "ZBAND": band,
        "ZMODE": "FT8",
        "ZLOCATOR": "PM02",
        "ZRSTTX": "-12",
        "ZRSTRX": "-12",
        "ZUUID": bytes.fromhex(f"{pk:032x}"),
    }


def test_full_merge_then_incremental(ft8_db_path, tmp_path: Path) -> None:
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(
        rumlog,
        [
            _rumlog_row("TL8GD", 27139, 1_795_384_049.0),
            _rumlog_row("BI4QMU", 27140, 1_795_384_100.0, band="40m"),
        ],
    )
    sender = CapturedSend([])
    report = run_sync_once(_cfg(ft8_db_path, rumlog), sender=sender)
    assert report.pulled == 2
    assert report.inserted == 2
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    # Both pulled records are now present with rumlog_uuid backfilled.
    rows = db._con.execute(
        "SELECT dx_call, rumlog_uuid, pushed_to_rumlog FROM qso ORDER BY id"
    ).fetchall()
    assert [(r["dx_call"], r["rumlog_uuid"]) for r in rows] == [
        ("TL8GD", f"{27139:032x}".upper()),
        ("BI4QMU", f"{27140:032x}".upper()),
    ]
    assert all(r["pushed_to_rumlog"] == 0 for r in rows)
    # Nothing pushed: pulled records are already on the RUMLogNG side.
    assert sender.payloads == []
    # Second round is a no-op (cursor advanced).
    report2 = run_sync_once(_cfg(ft8_db_path, rumlog), sender=sender)
    assert report2.pulled == 0
    assert report2.inserted == 0


def test_conflict_resolution_prefers_rumlog(ft8_db_path, tmp_path: Path) -> None:
    # Pre-existing FT8 record (from jtdx import) with different grid/RST.
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.insert_record(
        {
            "my_call": "BG1SB", "my_grid": "ON80DA", "dx_call": "TL8GD",
            "dx_grid": "", "report_sent": -10, "report_rcvd": -10,
            "started_utc": "232300", "mode": "FT8", "freq_hz": 14_074_684,
            "band": "20m", "status": "completed",
            "completed_epoch": 1_795_384_049.0, "rumlog_uuid": "", "source": "jtdx",
        }
    )
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(
        rumlog, [_rumlog_row("TL8GD", 27139, 1_795_384_049.0)]
    )
    report = run_sync_once(_cfg(ft8_db_path, rumlog), sender=CapturedSend([]))
    assert report.inserted == 0
    assert report.updated == 1
    row = db.get_record(1)
    assert row["dx_grid"] == "PM02"
    assert row["report_sent"] == -12
    assert row["rumlog_uuid"] == f"{27139:032x}".upper()


def test_push_new_ft8_record_and_confirm_next_round(ft8_db_path, tmp_path: Path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.insert_record(
        {
            "my_call": "BG1SB", "my_grid": "ON80DA", "dx_call": "V85T",
            "dx_grid": "", "report_sent": None, "report_rcvd": None,
            "started_utc": "162730", "mode": "FT8", "freq_hz": 14_076_000,
            "band": "20m", "status": "completed",
            "completed_epoch": 1_753_351_889.0, "rumlog_uuid": "", "source": "live",
        }
    )
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(rumlog, [])  # RUMLogNG empty for round 1
    sender = CapturedSend([])
    report = run_sync_once(_cfg(ft8_db_path, rumlog), sender=sender)
    assert report.pushed == 1
    assert sender.payloads[0].startswith(b"<MessageType:9>HEARTBEAT")
    assert b"QSO_LOGGED" in sender.payloads[1]
    assert b"<CALL:4>V85T" in sender.payloads[1]
    row = db.get_record(1)
    assert row["pushed_to_rumlog"] == 1
    # Round 2: RUMLogNG now contains the QSO → uuid backfilled, confirmed.
    make_rumlog_db(
        rumlog,
        [
            {
                "ZCALLSIGN": "V85T", "Z_PK": 1,
                "unix_epoch": 1_753_351_889.0, "ZQRG": 14.076,
                "ZBAND": "20m", "ZMODE": "FT8", "ZLOCATOR": "",
                "ZRSTTX": None, "ZRSTRX": None,
                "ZUUID": bytes.fromhex("11111111111111111111111111111111"),
            }
        ],
    )
    report2 = run_sync_once(_cfg(ft8_db_path, rumlog), sender=sender)
    assert report2.updated == 1  # conflict-resolved (same record) → uuid backfill
    row2 = db.get_record(1)
    assert row2["rumlog_uuid"] == "11111111111111111111111111111111"
    # Confirmed: the unconfirmed-push counter for qso 1 was cleared.
    assert json.loads(db.state_get("push_pending") or "{}") == {}


def test_unconfirmed_push_requeued_after_retries(ft8_db_path, tmp_path: Path) -> None:
    db = Ft8Db(ft8_db_path)
    db.ensure_schema()
    db.insert_record(
        {
            "my_call": "BG1SB", "my_grid": "ON80DA", "dx_call": "AA5AT",
            "dx_grid": "", "report_sent": None, "report_rcvd": None,
            "started_utc": "235833", "mode": "FT8", "freq_hz": 24_916_000,
            "band": "12m", "status": "completed",
            "completed_epoch": 1_727_952_233.0, "rumlog_uuid": "", "source": "live",
        }
    )
    rumlog = tmp_path / "log.sqlite"
    make_rumlog_db(rumlog, [])
    cfg = _cfg(ft8_db_path, rumlog, retries=2)
    for _ in range(3):
        run_sync_once(cfg, sender=CapturedSend([]))
    # retries=2: round 2 ages the counter, round 3 requeues and re-pushes.
    row = db.get_record(1)
    assert row["pushed_to_rumlog"] == 1  # requeued then re-pushed in round 3
    pending = json.loads(db.state_get("push_pending") or "{}")
    assert int(pending["1"]) == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_sync.py -v`
预期：FAIL（ModuleNotFoundError）

- [ ] **步骤 3：实现 sync.py**

```python
"""One sync round: pull RUMLogNG → merge into FT8 db; push FT8 → RUMLogNG.

Spec §3.2.  Pull: read-only cursor past ``last_seen_pk``, map each row,
dedupe against the FT8 db (UUID first, then dx_call+band with a 120 s
window), insert new or update existing with RUMLogNG winning.  Push:
records with ``pushed_to_rumlog=0`` go out as WSJT-X UDP HEARTBEAT +
QSO_LOGGED; unconfirmed pushes are requeued after ``confirm_retries``
rounds.  Every step is transactional per record; failures are logged and
skip that record, never aborting the round mid-way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .ft8_db import Ft8Db
from .mapper import build_push_adif_fields, map_rumlog_row
from .rumlog_reader import RUMlogReader
from .wsjt_udp import build_heartbeat, build_qso_logged, send_payload

log = logging.getLogger(__name__)

Sender = Callable[[bytes, str, int], None]


@dataclass
class SyncReport:
    pulled: int = 0
    inserted: int = 0
    updated: int = 0
    pushed: int = 0
    requeued: int = 0
    errors: list[str] = field(default_factory=list)


def run_sync_once(cfg: dict[str, object], *, sender: Sender = send_payload) -> SyncReport:
    """Execute one full pull+merge+push round; returns a report."""

    report = SyncReport()
    ft8 = Ft8Db(str(cfg["ft8_db"]))
    ft8.ensure_schema()
    try:
        reader = RUMlogReader(str(cfg["rumlog_db"]))
    except OSError as exc:
        report.errors.append(f"cannot open RUMLogNG db: {exc}")
        ft8.close()
        return report

    my_call = str(cfg["my_call"])
    my_grid = str(cfg["my_grid"])
    try:
        # ---- Step 1: pull -------------------------------------------------
        last_pk = int(ft8.state_get("last_seen_pk") or 0)
        for row in reader.fetch_since(last_pk):
            pk = int(row["Z_PK"])
            rec = map_rumlog_row(row, my_call=my_call, my_grid=my_grid)
            uuid_hex = str(rec["rumlog_uuid"])
            existing = ft8.find_by_uuid(uuid_hex) if uuid_hex else None
            if existing is None:
                existing = ft8.find_existing(
                    str(rec["dx_call"]), str(rec["band"]), float(rec["completed_epoch"])
                )
            if existing is None:
                ft8.insert_record(rec)
                report.inserted += 1
                log.info("pulled new QSO %s %s", rec["dx_call"], rec["band"])
            else:
                # RUMLogNG wins on its fields; keep FT8 completion times.
                merged = dict(rec)
                ft8.update_from_rumlog(existing, merged)
                report.updated += 1
                log.info("updated QSO %s from RUMLogNG", rec["dx_call"])
                # Push confirmation: RUMLogNG now holds this QSO.
                existing_row = ft8.get_record(existing)
                if existing_row and existing_row["pushed_to_rumlog"] == 1:
                    ft8.confirm_uuid(existing)
            report.pulled += 1
            last_pk = max(last_pk, pk)
        ft8.state_set("last_seen_pk", str(last_pk))

        # ---- Step 2: push --------------------------------------------------
        report.requeued = ft8.tick_unconfirmed(int(cfg["confirm_retries"]))
        pending = ft8.pending_push_records()
        if pending:
            sender(
                build_heartbeat(str(cfg["udp_id"])),
                str(cfg["udp_host"]),
                int(cfg["udp_port"]),
            )
        for rec in pending:
            payload = build_qso_logged(build_push_adif_fields(rec))
            sender(payload, str(cfg["udp_host"]), int(cfg["udp_port"]))
            ft8.mark_pushed(int(rec["id"]))
            report.pushed += 1
            log.info("pushed QSO %s to RUMLogNG", rec["dx_call"])

        ft8.record_audit(
            "rumlog_sync",
            f"pulled={report.pulled} inserted={report.inserted}"
            f" updated={report.updated} pushed={report.pushed}"
            f" requeued={report.requeued}",
        )
        ft8.commit()
    except Exception as exc:  # noqa: BLE001 — a round must never kill the crontab job
        report.errors.append(f"round failed: {exc!r}")
        log.exception("rumlog_sync round failed")
        ft8.rollback()
    finally:
        reader.close()
        ft8.close()
    return report
```

注意：`send_payload` 签名是 `(payload, host, port, *, sock=None)`，与 `Sender` 类型 `Callable[[bytes, str, int], None]` 兼容（关键字 sock 默认）；推送统一走注入的 `sender`（测试用 CapturedSend 捕获）。`tick_unconfirmed` 在推送前调用，超限记录本轮 requeue 并重推；Step 1 命中已推送记录时调 `confirm_uuid` 完成闭环确认。

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/rumlog_sync/ -v`
预期：PASS（含前 4 任务共 26 passed）

- [ ] **步骤 5：Commit**

```bash
git add rumlog_sync/sync.py tests/rumlog_sync/test_sync.py
git commit -m "feat(rumlog_sync): one-round pull/merge/push orchestration"
```

---

## 任务 7：**main**.py（CLI + flock + 冒烟旗标）

**文件：** 创建 `rumlog_sync/__main__.py`、`rumlog_sync/config.example.json`；测试 `tests/rumlog_sync/test_main_cli.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""__main__: CLI entry, flock guard, exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from rumlog_sync.__main__ import run_cli


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "rumlog-sync.json"
    cfg.write_text(
        json.dumps(
            {
                "ft8_db": str(tmp_path / "mrrc-ft8.db"),
                "rumlog_db": str(tmp_path / "missing.sqlite"),  # pull fails softly
                "lock_path": str(tmp_path / "sync.lock"),
            }
        )
    )
    return cfg


def test_run_cli_missing_config_uses_defaults(tmp_path: Path) -> None:
    code = run_cli(["--config", str(tmp_path / "nope.json")], cwd=str(tmp_path))
    assert code == 0  # RUMLogNG open failure is a soft error, exit 0


def test_run_cli_bad_config_exits_nonzero(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.json"
    cfg.write_text("{nope")
    code = run_cli(["--config", str(cfg)], cwd=str(tmp_path))
    assert code == 2


def test_run_cli_flock_prevents_concurrent_run(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    lock = tmp_path / "sync.lock"
    with lock.open("w") as stream:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = run_cli(["--config", str(cfg)], cwd=str(tmp_path))
        assert code == 3  # another instance holds the lock


def test_module_runs_as_program() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "rumlog_sync", "--help"],
        capture_output=True,
        text=True,
        cwd="/Users/cheenle/HAM/ft8",
    )
    assert proc.returncode == 0
    assert "--config" in proc.stdout
```

- [ ] **步骤 2：运行测试验证失败**

运行：`venv/bin/python -m pytest tests/rumlog_sync/test_main_cli.py -v`
预期：FAIL（ModuleNotFoundError）

- [ ] **步骤 3：实现 **main**.py 与配置模板**

```python
"""CLI entry: one sync round per invocation (crontab-driven, spec §11).

Exit codes: 0 = ok (including soft RUMLogNG-open failures, which log and
skip the round), 2 = bad config, 3 = another instance holds the lock.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config
from .sync import run_sync_once

log = logging.getLogger("rumlog_sync")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rumlog_sync", description=__doc__)
    parser.add_argument(
        "--config",
        default="data/rumlog-sync.json",
        help="config path (default data/rumlog-sync.json)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="push one test QSO to RUMLogNG and exit (manual validation only)",
    )
    return parser.parse_args(argv)


def run_cli(argv: list[str], *, cwd: str | None = None) -> int:
    """Entry point returning a process exit code (0/2/3)."""

    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except ValueError:
        log.exception("config load failed")
        return 2

    lock_path = Path(cfg["lock_path"])
    if not lock_path.is_absolute():
        lock_path = Path(cwd or ".") / lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_stream:
        try:
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.warning("another sync instance holds %s; skipping round", lock_path)
            return 3
        if args.smoke:
            _run_smoke(cfg)
            return 0
        report = run_sync_once(cfg)
        if report.errors:
            log.error("round errors: %s", report.errors)
        log.info(
            "round done: pulled=%d inserted=%d updated=%d pushed=%d requeued=%d",
            report.pulled, report.inserted, report.updated,
            report.pushed, report.requeued,
        )
        return 0


def _run_smoke(cfg: dict[str, object]) -> None:
    """Development validation: push a marker QSO to RUMLogNG (spec §6)."""

    from .mapper import build_push_adif_fields
    from .wsjt_udp import build_heartbeat, build_qso_logged, send_payload

    rec = {
        "dx_call": "N0SMK",
        "dx_grid": "",
        "mode": "FT8",
        "band": "20m",
        "freq_hz": 14_074_000,
        "report_sent": None,
        "report_rcvd": None,
        "started_utc": "000000",
        "completed_epoch": 0.0,
        "my_call": str(cfg["my_call"]),
    }
    # completed_epoch=0 renders an ADIF date of 1970 — RUMLogNG accepts
    # but this is a marker; delete it manually after validation.
    send_payload(
        build_heartbeat(str(cfg["udp_id"])), str(cfg["udp_host"]), int(cfg["udp_port"])
    )
    send_payload(
        build_qso_logged(build_push_adif_fields(rec)),
        str(cfg["udp_host"]),
        int(cfg["udp_port"]),
    )
    log.warning("smoke QSO sent to RUMLogNG — delete it manually after validation")


if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
```

`rumlog_sync/config.example.json`：

```json
{
  "ft8_db": "data/mrrc-ft8.db",
  "rumlog_db": "/Users/cheenle/Library/Containers/de.dl2rum.RUMlogNG/Data/Library/Application Support/RUMLogNG/CoreQsoModel_1.sqlite",
  "my_call": "BG1SB",
  "my_grid": "ON80DA",
  "udp_host": "127.0.0.1",
  "udp_port": 2237,
  "udp_id": "MRRC-FT8-SYNC",
  "confirm_retries": 3,
  "lock_path": "data/rumlog-sync.lock"
}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`venv/bin/python -m pytest tests/rumlog_sync/ -v`
预期：PASS（全部通过）

- [ ] **步骤 5：Commit**

```bash
git add rumlog_sync/__main__.py rumlog_sync/config.example.json tests/rumlog_sync/test_main_cli.py
git commit -m "feat(rumlog_sync): CLI entry with flock guard + smoke flag"
```

---

## 任务 8：文档同步（SDD / AGENTS / tests README / 约束登记）

**文件：** 修改 `SDD/08-architecture-decisions.md`、`SDD/12-operational-model.md`、`SDD/14-version-history.md`、`AGENTS.md`、`tests/README.md`、`.agents/skills/sdd-guardian/harness/constraints.json`、`.agents/skills/sdd-guardian/harness/index.json`

- [ ] **步骤 1：SDD/08 新增 AD-016**

在 `SDD/08-architecture-decisions.md` 末尾（其他 AD 之后）追加：

```markdown
## AD-016 — RUMLogNG bidirectional QSO sync (UDP push + read-only Core Data poll)

**Problem:** 本机同时运行 MRRC-FT8 headless 服务器（qso 表在 data/mrrc-ft8.db）与
RUMLogNG 主日志（Core Data SQLite），同一批 FT8 QSO 经不同路径进入两边，两边记录
集合与字段细节持续分歧；需要任一边新增时定期（5 min）双向同步。

**Decision:**
- FT8 db → RUMLogNG：WSJT-X 协议 UDP（HEARTBEAT + QSO_LOGGED，0x00 结尾）发往
  RUMLogNG 原生监听的 127.0.0.1:2237；推送状态与 RUMLogNG 侧 UUID 记录在 qso 表
  新增列（rumlog_uuid / pushed_to_rumlog），闭环确认 + 超轮重推。
- RUMLogNG → FT8 db：只读（uri mode=ro）轮询 CoreQsoModel_1.sqlite，Z_PK 游标
  增量；列存在性探测防御 RUMLogNG schema 升级；字段冲突以 RUMLogNG 为准，
  completed_epoch/started_utc 保留 FT8 侧精确值。
- 跨库去重：rumlog_uuid 精确匹配优先；否则 dx_call+band 相同且完成时刻差 ≤120 s。
- **禁止写 RUMLogNG 数据库**（Core Data 内部状态表外部写入会损坏日志）。

**Consequences:** 两边日志记录集合最终一致；RUMLogNG 侧删记录不回删 FT8 db
（单向追加语义）；同步程序独立于 server 运行（crontab 驱动，flock 防重入）。
```

- [ ] **步骤 2：SDD/12 §12.7 扩展**

在 `SDD/12-operational-model.md` §12.7 后追加一小节：

```markdown
**12.7.1 RUMLogNG sync state** — rumlog_sync（AD-016）在 qso 表维护
`rumlog_uuid`（RUMLogNG 侧 UUID）与 `pushed_to_rumlog`（推送状态），
`rumlog_sync_state` 表存 Z_PK 游标与未确认推送计数；迁移幂等
（PRAGMA 探测 + ADD COLUMN），与服务器 Repository 显式列查询向后兼容。
```

- [ ] **步骤 3：SDD/14-version-history.md 新条目**

在文件顶部（`## Unreleased` 序列最上方）追加：

```markdown
## Unreleased — 2026-08-11 — RUMLogNG bidirectional QSO sync (AD-016)

独立 `rumlog_sync` 包：WSJT-X UDP 2237 推送（HEARTBEAT+QSO_LOGGED）+ 只读
Core Data 轮询；Z_PK 游标增量、rumlog_uuid 幂等、120 s 去重窗口、以 RUMLogNG
为准的字段冲突规则、推送闭环确认与超轮重推；crontab */5 驱动 + flock 防重入；
qso 表新增 rumlog_uuid/pushed_to_rumlog 列（幂等迁移）。规格：
docs/superpowers/specs/2026-08-11-rumlogng-sync-design.md。
```

- [ ] **步骤 4：AGENTS.md 模块表加行**

在模块表（`server/engine/` 行之后）追加：

```markdown
| `rumlog_sync/` | 独立 QSO 双向同步（AD-016）：FT8 db ↔ RUMLogNG；WSJT-X UDP 2237 推送 + 只读 Core Data 轮询；Z_PK 游标、rumlog_uuid 幂等、120 s 去重窗口、RUMLogNG 为准、推送闭环重推；`python -m rumlog_sync` + crontab `*/5` + flock |
```

- [ ] **步骤 5：tests/README.md 追加段落**

```markdown
The RUMLogNG sync suite (`tests/rumlog_sync/`) covers config loading,
Core Data → FT8 mapping (timestamp offset, RST parsing, UUID hex), the
120 s dedupe predicate, read-only schema probe (missing-column rejection),
WSJT-X UDP message construction (HEARTBEAT + QSO_LOGGED with exact length
prefix and null terminator, injected fake socket — no real packets), the
idempotent qso-column migration, push state machine (mark/confirm/requeue),
and a full one-round integration (pull → merge → push → confirm). The
real-RUMLogNG smoke path is opt-in (`--smoke`, manual QSO + delete).
```

- [ ] **步骤 6：constraints.json / index.json 登记**

在 `.agents/skills/sdd-guardian/harness/constraints.json` 增加 block 规则（参考既有条目格式；先读该文件确认结构再插入）：

```json
{
  "id": "RUMLOG_READONLY",
  "severity": "block",
  "pattern": "rumlog_sync.*(mode=rw|DELETE FROM ZCORE_QSO|UPDATE ZCORE_QSO|INSERT INTO ZCORE_QSO)",
  "message": "RUMLogNG Core Data 数据库只读访问（AD-016）；禁止直接写 Core Data"
}
```

`index.json` 增加 topics 映射 `rumlog_sync` → `["AD-016", "12.7"]`（先读该文件确认结构）。

- [ ] **步骤 7：验证 harness + 全量测试**

运行：

```bash
venv/bin/python -m pytest tests/ -x -q
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
```

预期：pytest 全绿；`check --staged` 打印 `clean`

- [ ] **步骤 8：Commit**

```bash
git add SDD/ AGENTS.md tests/README.md .agents/skills/sdd-guardian/harness/
git commit -m "docs: AD-016 RUMLogNG sync + module table + test inventory + constraint registry"
```

---

## 任务 9：端到端验证 + 部署说明

**文件：** 创建 `README` 段或 `docs/rumlog-sync.md`（部署说明）

- [ ] **步骤 1：手动端到端验证（真实环境，不进 pytest）**

```bash
cd /Users/cheenle/HAM/ft8
cp data/mrrc-ft8.db /tmp/mrrc-ft8-backup.db   # safety copy
venv/bin/python -m rumlog_sync --config data/rumlog-sync.json
```

预期：首轮全量合并日志（pulled=… inserted=… updated=…）；`sqlite3 data/mrrc-ft8.db "SELECT COUNT(*) FROM qso WHERE source='rumlog';"` 增加；`rumlog_sync_state` 表有 last_seen_pk。

RUMLogNG 侧验证：打开 RUMLogNG 日志窗口，确认 FT8 db 独有的历史 QSO（如 V85T）出现在 RUMLogNG 中。

- [ ] **步骤 2：回滚方案确认**

若首轮合并后 RUMLogNG 出现异常记录：

```bash
cp /tmp/mrrc-ft8-backup.db data/mrrc-ft8.db   # 恢复 FT8 db
# RUMLogNG 侧手动删除误入记录（RUMLogNG UI 删除；禁写 Core Data）
```

- [ ] **步骤 3：写部署说明 `docs/rumlog-sync.md`**

```markdown
# RUMLogNG 双向同步 — 部署说明

## 安装 crontab

crontab -e 添加（每 5 分钟一轮；flock 防重入）：

    */5 * * * * cd /Users/cheenle/HAM/ft8 && flock -n data/rumlog-sync.lock venv/bin/python -m rumlog_sync >> data/rumlog-sync.log 2>&1

## 配置

data/rumlog-sync.json（缺失时用默认值；模板见 rumlog_sync/config.example.json）。
关键项：rumlog_db（RUMLogNG Core Data 路径）、my_call/my_grid、confirm_retries。

## 首次运行

venv/bin/python -m rumlog_sync   # 自动全量合并两边历史（规格 D2）

## 冒烟验证（可选）

venv/bin/python -m rumlog_sync --smoke   # 推一条标记 QSO 验证落库；验证后手动删除

## 退出码

0 = 完成（含 RUMLogNG 打开失败的软错误）；2 = 配置错误；3 = 另一实例持锁。
```

- [ ] **步骤 4：Commit**

```bash
git add docs/rumlog-sync.md
git commit -m "docs: RUMLogNG sync deploy guide (crontab + first-run + smoke)"
```

---

## 自检记录

- **规格覆盖度**：§3.1 形态→任务 1/7；§3.2 流程→任务 6；§4 映射→任务 2；§5 去重→任务 2/5；§6 UDP→任务 4/7(--smoke)；§7 错误处理→任务 6（软错误+audit）+任务 7（退出码）；§8 迁移→任务 5；§9 测试→任务 2–7；§10 文档同步→任务 8；§11 部署→任务 9。§12 范围外未实现（符合 YAGNI）。
- **类型一致性**：`record dict` 键名（dx_call/band/completed_epoch/rumlog_uuid…）在 mapper/ft8_db/sync 三处一致；`Sender` 签名 `(payload, host, port)` 与 `send_payload(payload, host, port, *, sock=None)` 兼容，推送统一走注入 `sender`（测试可捕获）；推送状态机统一为 `mark_pushed` / `tick_unconfirmed(confirm_retries)` / `confirm_uuid`（任务 5 定义，任务 6 使用），`pending_push_records` 含 `id` 列；`Ft8Db.rollback` 在任务 5 定义、任务 6 使用；确认闭环：Step 1 更新分支命中 `pushed_to_rumlog=1` 记录时调 `confirm_uuid`。
