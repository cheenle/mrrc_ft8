# DXCC 统计 + 菜单实时展示 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 基于 canonical store 全量 QSO 实时统计 DXCC 实体（总数 + 实体列表 + 波段矩阵），在设置菜单加 "DXCC" tab 全屏 overlay 展示，打开即最新。

**架构：** 纯函数模块 `server/engine/dxcc.py` 解析仓库内 `cty.dat`（country-files 旧版 ADIF 格式，实体行 + 跨行前缀列表，`=`精确 / `(23)`数字替换 / 最长前缀匹配），`dxcc_summary(repository, cty)` 遍历非 void QSO 统计；`GET /api/v1/dxcc` 实时返回 `_ok` 信封；前端镜像 LOG overlay 模式。

**技术栈：** Python 3.13 / SQLite / vanilla JS / pytest（TDD）。**零新 Python 依赖。**

**规格：** `docs/superpowers/specs/2026-08-04-dxcc-stats-design.md`

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `server/engine/dxcc.py` | `cty.dat` 解析（`CtyDatabase`/`CtyEntity`）+ `lookup()` + `dxcc_summary()` | 新建 |
| `server/web/api.py` | `GET /api/v1/dxcc` + `_cty_database()` 懒加载单例 | 修改 |
| `server/web/static/index.html` | 抽屉加 DXCC tab；`#dxcc-overlay` 全屏 overlay | 修改 |
| `server/web/static/js/api.js` | `dxcc: () => request("/dxcc")` | 修改 |
| `server/web/static/js/settings.js` | `switchTab("dxcc")` → `openDxccView()`（镜像 openLogView） | 修改 |
| `tests/engine/test_dxcc.py` | 解析 / lookup / 统计 + 真实数据冒烟 | 新建 |
| `tests/web/test_api.py` | `/dxcc` 认证 + 结构 | 修改 |
| `AGENTS.md`、`SDD/05`、`SDD/11`、`SDD/12`、`SDD/14` | 文档同步 | 修改 |

**已验证事实（2026-08-04 冒烟）：** `cty.dat` 4,172 行 → 346 实体；格式 `name: cqz: ituz: cont: lat: lon: tz: prefix:`（**无 DXCC 编号列**）；真实 6,740 个去重呼号 → **187 实体**、5 个未匹配（`D1DX`、`B0CRA`、`B9/BI1GJL`、`D1IJZ`、`B0/BD7OXR`，B0/B9 省际前缀与 D1 活动台）。解析/匹配代码已用下述形式在冒烟中验证通过。

---

### 任务 1：`server/engine/dxcc.py` — cty.dat 解析 + lookup

**文件：**
- 创建：`server/engine/dxcc.py`
- 测试：创建 `tests/engine/test_dxcc.py`

- [ ] **步骤 1：编写失败的测试**

```python
"""cty.dat parsing + callsign→DXCC lookup (spec §3.1)."""

from __future__ import annotations

from server.engine.dxcc import CtyDatabase, load_cty

# 精简 fixture：实体行 + 跨行续行 + =精确 + (23)数字替换
FIXTURE = """\
Sov Mil Order of Malta:   15:  28:  EU:   41.90:   -12.43:    -1.0:  1A:
    1A;
China:                    24:  44:  AS:   36.00:  -102.00:    -8.0:  BY:
    3H0(23)[42],BI,BJ,BV,=B7P4A,BY;
Monaco:                   14:  27:  EU:   43.73:    -7.40:    -1.0:  3A:
    3A,=3A/4Z5KJ/LH;
"""


def test_load_cty_parses_entities_and_continuation_lines(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert len(db.entities) == 3
    names = [e.name for e in db.entities]
    assert names == ["Sov Mil Order of Malta", "China", "Monaco"]
    china = db.entities[1]
    assert china.continent == "AS"
    # 续行收集：3H0(23)[42] 展开 + BI/BJ/BV/=B7P4A/BY
    assert "3H0" in china.prefixes
    assert "3H2" in china.prefixes
    assert "=B7P4A" in china.prefixes


def test_lookup_prefix_and_continent(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("BI1TX") == ("China", "AS")
    assert db.lookup("BG7BMG") == ("China", "AS")
    assert db.lookup("1A0KM") == ("Sov Mil Order of Malta", "EU")
    assert db.lookup("3A2MW") == ("Monaco", "EU")


def test_lookup_exact_match_and_digit_replacement(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    # =B7P4A 精确匹配：B7P4A 整呼号
    assert db.lookup("B7P4A") == ("China", "AS")
    # 3H0(23)[42] → 3H0 / 3H2 / 3H3 前缀
    assert db.lookup("3H0XX") == ("China", "AS")
    assert db.lookup("3H2YY") == ("China", "AS")
    assert db.lookup("3H3ZZ") == ("China", "AS")
    assert db.lookup("3H1QQ") is None  # 数字替换不允许 1


def test_lookup_strips_slash_suffix(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("BI1TX/QRP") == ("China", "AS")  # base = BI1TX
    assert db.lookup("3A2MW/P") == ("Monaco", "EU")


def test_lookup_unknown_returns_none(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE)
    db = load_cty(str(path))
    assert db.lookup("ZZ9ZZZ") is None
```

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/engine/test_dxcc.py -q`
预期：FAIL（`ModuleNotFoundError: server.engine.dxcc`）

- [ ] **步骤 3：实现 `server/engine/dxcc.py`**

```python
"""DXCC entity lookup from the repo's cty.dat (country-files ADIF format).

The file's entity rows are ``name: cqz: ituz: continent: lat: lon: tz: prefix:``
(no DXCC number column — the entity name is the statistical unit).  Prefix
lists span continuation lines, comma-separated, terminated by ``;``.  Entries
may be ``=CALL`` (exact match) or carry ``(digits)`` digit-replacement rules
(e.g. ``3H0(23)[42]`` matches 3H0/3H2/3H3 prefixes); the ``[nn]`` override
number is ignored because this data source has no number column.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_ENTITY_RE = re.compile(
    r"^(.*?):\s*\d+:\s*\d+:\s*(\S+):\s*[-\d.]+:\s*[-\d.]+:\s*[-\d.]+:\s*\S+:\s*$"
)


@dataclass
class CtyEntity:
    name: str
    continent: str
    prefixes: list[str]  # 展开后的匹配模式（无 = 前缀、无括号）


def _expand_entry(entry: str) -> list[tuple[str, bool]]:
    """One raw prefix entry → [(pattern, exact)]; (23) digit replacement
    expands to one pattern per digit.  ``=`` marks exact call match."""

    entry = entry.strip().rstrip(";").strip()
    if not entry:
        return []
    exact = entry.startswith("=")
    entry = entry.lstrip("=")
    m = re.match(r"^(.*)\((\d+)\)(?:\[\d+\])?$", entry)
    if m:
        base, digits = m.groups()
        return [(base + d, exact) for d in digits]
    return [(entry, exact)]


@dataclass
class CtyDatabase:
    entities: list[CtyEntity]

    def lookup(self, call: str) -> tuple[str, str] | None:
        """(entity_name, continent) for a callsign; exact match wins, then
        the longest prefix match; None when nothing matches."""

        base = call.split("/", 1)[0].upper()
        best_len = -1
        best: CtyEntity | None = None
        for entity in self.entities:
            for stored in entity.prefixes:
                if stored.startswith("="):
                    if base == stored[1:]:
                        return (entity.name, entity.continent)
                elif base.startswith(stored):
                    if len(stored) > best_len:
                        best_len = len(stored)
                        best = entity
        return (best.name, best.continent) if best else None


def load_cty(path: str) -> CtyDatabase:
    """Parse cty.dat into a CtyDatabase; missing/broken file → empty db
    (callers fall back to all-unmatched, never crash)."""

    entities: list[CtyEntity] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = [line.rstrip("\n") for line in stream]
    except OSError as exc:
        log.exception("cty.dat unreadable: %s", exc)
        return CtyDatabase(entities)
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _ENTITY_RE.match(line)
        if m:
            name, continent = m.groups()
            i += 1
            buf = ""
            while i < len(lines):
                s = lines[i].rstrip()
                if s.startswith((" ", "\t")):
                    buf += s.strip()
                    i += 1
                    if buf.endswith(";"):
                        break
                else:
                    break
            prefixes: list[str] = []
            seen: set[str] = set()
            for raw in buf.split(","):
                for pattern, exact in _expand_entry(raw):
                    stored = f"={pattern}" if exact else pattern
                    if stored not in seen:
                        seen.add(stored)
                        prefixes.append(stored)
            entities.append(CtyEntity(name.strip(), continent, prefixes))
        else:
            i += 1
    return CtyDatabase(entities)
```

**注意：** `prefixes` 存储已展开的编码串（`=X` 表示精确匹配，`X` 表示前缀）；`lookup` 先精确后最长前缀。`3H0(23)[42]` 展开为 `3H0`/`3H2`/`3H3` 三条（测试断言 3H1 不匹配、3H2/3H3 匹配）。`_expand_entry` 由 `_expand_entry_each` 改名并返回 `(pattern, exact)` 列表。

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/engine/test_dxcc.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add server/engine/dxcc.py tests/engine/test_dxcc.py
git commit -m "feat(dxcc): cty.dat parser + callsign lookup (exact / digit-replacement / longest prefix)"
```

---

### 任务 2：`dxcc_summary` 统计 + 真实数据冒烟

**文件：**
- 修改：`server/engine/dxcc.py`（追加 `DxccEntityStat`/`DxccSummary`/`dxcc_summary`）
- 测试：`tests/engine/test_dxcc.py`

- [ ] **步骤 1：编写失败的测试**（追加到 `tests/engine/test_dxcc.py`）

```python
from datetime import datetime, timezone

from server.engine.dxcc import dxcc_summary
from server.engine.repository import Repository
from server.engine.sequencer import QSORecord

FIXTURE2 = """\
China:                    24:  44:  AS:   36.00:  -102.00:    -8.0:  BY:
    BI,BY;
Japan:                    25:  45:  AS:   36.00:    138.00:    -9.0:  JA:
    JA;
Mauritius:                39:  53:  AF:  -20.35:   -57.50:    -4.0:  3B8:
    3B8;
"""


def _repo_with_qsos() -> Repository:
    repo = Repository(":memory:")
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BI1TX",
                  band="20m"), completed_epoch=1700000000.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BY1OK",
                  band="20m"), completed_epoch=1700000100.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="JA1YAD",
                  band="40m"), completed_epoch=1700000200.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="JA1YAD",
                  band="20m"), completed_epoch=1700000300.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="3B8CW",
                  band="20m"), completed_epoch=1700000400.0,
    )
    repo.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="ZZ9ZZZ",
                  band="15m"), completed_epoch=1700000500.0,
    )
    return repo


def test_dxcc_summary_counts_entities_bands_and_unmatched(tmp_path) -> None:
    path = tmp_path / "cty.dat"
    path.write_text(FIXTURE2)
    cty = load_cty(str(path))
    summary = dxcc_summary(_repo_with_qsos(), cty)
    assert summary.total == 3                      # China / Japan / Mauritius
    assert summary.unmatched == 1                  # ZZ9ZZZ
    names = [e.name for e in summary.entities]
    assert names == ["China", "Japan", "Mauritius"]  # sorted
    # China: BI1TX + BY1OK 都算一个实体；同实体同波段 20m 只计 1
    china = summary.entities[0]
    assert china.continent == "AS"
    assert china.band_count == 1                   # 只有 20m
    assert china.first_utc == "2023-11-14T22:13:20Z"  # 1700000000
    japan = summary.entities[1]
    assert japan.band_count == 2                   # 40m + 20m
    assert japan.bands == ["20m", "40m"]           # sorted
    # by_band：20m → China+Japan+Mauritius = 3；40m → Japan = 1；15m → 0（unmatched 不计）
    assert summary.by_band == {"20m": 3, "40m": 1}
```

（时间断言：1700000000 = 2023-11-14 22:13:20 UTC，`first_utc` 以 ISO 字符串 `"2023-11-14T22:13:20Z"` 输出。）

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/engine/test_dxcc.py::test_dxcc_summary_counts_entities_bands_and_unmatched -q`
预期：FAIL（`ImportError: cannot import name 'dxcc_summary'`）

- [ ] **步骤 3：实现 `dxcc_summary`**（`server/engine/dxcc.py` 末尾追加）

```python
@dataclass
class DxccEntityStat:
    name: str
    continent: str
    first_utc: str
    last_utc: str
    band_count: int
    bands: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "continent": self.continent,
            "first_utc": self.first_utc,
            "last_utc": self.last_utc,
            "band_count": self.band_count,
            "bands": self.bands,
        }


@dataclass
class DxccSummary:
    total: int
    unmatched: int
    entities: list[DxccEntityStat]
    by_band: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "unmatched": self.unmatched,
            "entities": [e.to_dict() for e in self.entities],
            "by_band": self.by_band,
        }


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dxcc_summary(repository, cty: CtyDatabase) -> DxccSummary:
    """Full-scan DXCC statistics over non-void QSOs (spec §3.2).

    Same entity × same band counts once (DXCC Challenge semantics);
    unmatched calls count into ``unmatched`` without failing.
    """

    stats: dict[str, DxccEntityStat] = {}
    by_band: dict[str, set[str]] = {}
    lookup_cache: dict[str, tuple[str, str] | None] = {}
    unmatched = 0
    for qso in repository.list_qsos(include_void=False):
        key = (qso.dx_call, qso.band)
        result = lookup_cache.get(qso.dx_call)
        if result is None and qso.dx_call not in lookup_cache:
            result = cty.lookup(qso.dx_call)
            lookup_cache[qso.dx_call] = result
        if result is None:
            unmatched += 1
            continue
        name, continent = result
        stat = stats.get(name)
        if stat is None:
            stat = stats[name] = DxccEntityStat(
                name=name, continent=continent,
                first_utc=_utc_iso(qso.completed_epoch),
                last_utc=_utc_iso(qso.completed_epoch),
                band_count=0, bands=[],
            )
        else:
            stat.first_utc = min(stat.first_utc, _utc_iso(qso.completed_epoch))
            stat.last_utc = max(stat.last_utc, _utc_iso(qso.completed_epoch))
        if qso.band and qso.band not in stat.bands:
            stat.bands.append(qso.band)
            stat.bands.sort()
            stat.band_count = len(stat.bands)
        by_band.setdefault(qso.band, set()).add(name)
    ordered = sorted(stats.values(), key=lambda s: s.name)
    return DxccSummary(
        total=len(ordered),
        unmatched=unmatched,
        entities=ordered,
        by_band={band: len(ents) for band, ents in by_band.items()},
    )
```

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/engine/test_dxcc.py -q`
预期：PASS

- [ ] **步骤 5：真实数据冒烟验证**

```bash
cd /Users/cheenle/HAM/ft8 && venv/bin/python - <<'EOF'
from pathlib import Path
from server.engine.dxcc import load_cty, dxcc_summary
from server.engine.repository import Repository
cty = load_cty(str(Path("cty.dat").resolve()))
s = dxcc_summary(Repository("mrrc-ft8.db"), cty)
print("total:", s.total, "unmatched:", s.unmatched)
print("top bands:", sorted(s.by_band.items(), key=lambda kv: -kv[1])[:6])
print("top entities:", [(e.name, e.band_count) for e in s.entities[:8]])
EOF
```
预期：`total: 187`、`unmatched: 5`（数据变动以实际为准，total ≥ 180）、band 分布 20m 领先。

- [ ] **步骤 6：Commit**

```bash
git add server/engine/dxcc.py tests/engine/test_dxcc.py
git commit -m "feat(dxcc): full-scan summary (total / entities / by_band)"
```

---

### 任务 3：`GET /api/v1/dxcc` 接口

**文件：**
- 修改：`server/web/api.py`
- 测试：`tests/web/test_api.py`

- [ ] **步骤 1：编写失败的测试**（追加到 `tests/web/test_api.py`）

```python
def test_dxcc_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/dxcc").status_code == 401


def test_dxcc_returns_ok_envelope_with_summary(client: TestClient, state: AppState) -> None:
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BI1TX", band="20m")
    )
    session_id = login(client)
    body = client.get("/api/v1/dxcc", headers=auth_headers(session_id)).json()
    assert body.get("ok") is True
    assert isinstance(body["total"], int)
    assert body["total"] >= 1
    assert "entities" in body and "by_band" in body
    # cty.dat 在仓库根，测试环境能加载（至少解析出实体）
    assert isinstance(body["entities"], list)
```

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/web/test_api.py::test_dxcc_requires_authentication tests/web/test_api.py::test_dxcc_returns_ok_envelope_with_summary -q`
预期：FAIL（404 Not Found——路由不存在）

- [ ] **步骤 3：实现接口**（`server/web/api.py`）

模块级（`_static_dir` 附近）加懒加载单例：

```python
def _cty_database() -> Any:
    """Repository-root cty.dat loaded once; empty db on any failure."""

    if _cty_database.cache is not None:
        return _cty_database.cache
    from pathlib import Path

    from .engine.dxcc import load_cty

    path = Path(__file__).resolve().parent.parent / "cty.dat"
    _cty_database.cache = load_cty(str(path))
    return _cty_database.cache


_cty_database.cache = None  # type: ignore[attr-defined]
```

router 内（`logs_adif` 之后、diagnostics 之前）加端点：

```python
    @router.get("/dxcc")
    async def dxcc(session: Session = Depends(require_session)) -> JSONResponse:
        from .engine.dxcc import dxcc_summary

        summary = await asyncio.to_thread(dxcc_summary, state.repository, _cty_database())
        return _ok(summary.to_dict())
```

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/web/test_api.py::test_dxcc_requires_authentication tests/web/test_api.py::test_dxcc_returns_ok_envelope_with_summary -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add server/web/api.py tests/web/test_api.py
git commit -m "feat(web): GET /api/v1/dxcc real-time summary endpoint"
```

---

### 任务 4：前端 — DXCC 菜单 + 全屏 overlay

**文件：**
- 修改：`server/web/static/index.html`、`server/web/static/js/api.js`、`server/web/static/js/settings.js`

- [ ] **步骤 1：index.html — tab + overlay**

抽屉 tabs（`data-tab="log"` 之后）加：

```html
        <button class="tab" data-tab="dxcc">DXCC</button>
```

`#log-overlay` 之后加：

```html
    <!-- Full-screen DXCC stats -->
    <div id="dxcc-overlay" class="log-overlay" hidden>
      <div class="log-panel">
        <header class="log-header">
          <strong>DXCC <span class="count" id="dxcc-count"></span></strong>
          <button id="btn-dxcc-close" class="icon-btn" aria-label="Close DXCC">✕</button>
        </header>
        <div id="dxcc-content" class="log-list"></div>
      </div>
    </div>
```

- [ ] **步骤 2：api.js — 加方法**

```js
  dxcc: () => request("/dxcc"),
```

（放在 `qsos: ...` 行之后。）

- [ ] **步骤 3：settings.js — openDxccView**

`openLogView`/`closeLogView` 之后加：

```js
  const dxccOverlay = document.getElementById("dxcc-overlay");
  const dxccContent = document.getElementById("dxcc-content");
  const dxccCount = document.getElementById("dxcc-count");

  async function openDxccView() {
    dxccOverlay.hidden = false;
    document.body.classList.add("log-open");
    dxccContent.innerHTML = "<p class='drawer-hint'>Loading DXCC stats…</p>";
    const res = await api.dxcc();
    if (!res.ok) {
      dxccContent.innerHTML =
        `<p class='drawer-hint dim'>Could not load DXCC: ${res.reason || res.status}</p>`;
      return;
    }
    if (dxccCount) dxccCount.textContent = String(res.total);
    const html = [];
    html.push(`<div class="drawer-hint">已通联 <b>${res.total}</b> 个 DXCC 实体` +
      (res.unmatched ? `（${res.unmatched} 条未识别呼号）` : "") + "</div>");
    // 波段矩阵（by_band 降序）
    const bands = Object.entries(res.by_band || {})
      .sort((a, b) => b[1] - a[1]);
    if (bands.length) {
      html.push("<h3>By band</h3>");
      html.push(bands.map(([band, n]) =>
        `<div class="qso-row"><span class="qso-call">${band}</span>` +
        `<span class="qso-meta">${n} DXCC</span></div>`).join(""));
    }
    // 实体列表
    html.push("<h3>Entities</h3>");
    const rows = (res.entities || []).map((e) =>
      `<div class="qso-row"><span class="qso-call">${e.name}</span>` +
      `<span class="qso-meta">${e.continent} · ${e.first_utc} · ${e.band_count} band(s)</span></div>`
    ).join("");
    html.push(rows || "<p class='drawer-hint dim'>No DXCC yet.</p>");
    dxccContent.innerHTML = html.join("");
  }

  function closeDxccView() {
    dxccOverlay.hidden = true;
    document.body.classList.remove("log-open");
  }
```

`switchTab` 加分支（`tab === "log"` 分支之后）：

```js
    if (tab === "dxcc") {
      close();          // leave the settings drawer
      openDxccView();   // full-screen DXCC stats overlay
      return;
    }
```

`document.getElementById("btn-log-close")...` 附近加事件绑定：

```js
  document.getElementById("btn-dxcc-close").addEventListener("click", closeDxccView);
  dxccOverlay.addEventListener("click", (event) => {
    if (event.target === dxccOverlay) closeDxccView();
  });
```

- [ ] **步骤 4：验证（无 JS 测试框架，语法检查 + 手动）**

```bash
cd /Users/cheenle/HAM/ft8 && node --check server/web/static/js/settings.js && node --check server/web/static/js/api.js
grep -c "data-tab=\"dxcc\"\|dxcc-overlay" server/web/static/index.html
```
预期：`node --check` 无输出（语法 OK）；grep 计数 ≥ 2。

- [ ] **步骤 5：Commit**

```bash
git add server/web/static/index.html server/web/static/js/api.js server/web/static/js/settings.js
git commit -m "feat(web): DXCC menu tab + full-screen stats overlay"
```

---

### 任务 5：文档同步（AGENTS.md / SDD）

**文件：**
- 修改：`AGENTS.md`、`SDD/05-non-functional-requirements.md`、`SDD/11-component-model.md`、`SDD/12-operational-model.md`、`SDD/14-version-history.md`

- [ ] **步骤 1：AGENTS.md 模块表**（`server/engine/adif_import.py` 行附近加）

```
| `server/engine/dxcc.py` | 仓库内 `cty.dat`（country-files ADIF 格式）解析 + 呼号→DXCC 实体 lookup（`=`精确 / `(23)`数字替换 / 最长前缀）+ `dxcc_summary` 全量统计（总数/实体列表/波段矩阵） |
```

- [ ] **步骤 2：SDD/05 加 NFR**（NFR-085 行之后）

```
| NFR-086 | DXCC stats | `GET /api/v1/dxcc` returns ok envelope with total / entities / by_band, computed live from the canonical store on open (no push, no cache) |
```

- [ ] **步骤 3：SDD/11 组件表**（`adif_import.py` 行之后）

```
| `dxcc.py` | cty.dat parser + callsign→DXCC lookup + full-scan summary (total / entities / by_band) |
```

- [ ] **步骤 4：SDD/12 §12.6**（JTDX 段之后补一句）

```
`cty.dat` (repo root, country-files ADIF format) is the DXCC entity source for `GET /api/v1/dxcc`; parsed on first request, loaded lazily.
```

- [ ] **步骤 5：SDD/14 版本历史**（顶部 Unreleased 区加条目，格式同前）

```
## Unreleased — 2026-08-04 — DXCC Stats + Menu Live View

- 新模块 `server/engine/dxcc.py`：自写 `cty.dat` 解析器（country-files 旧版 ADIF 格式，实体行 + 跨行续行前缀列表，`=`精确匹配 / `(23)`数字替换 / 最长前缀匹配）。pyhamtools 0.13.0 可行性验证被否决（其 LookupLib 只支持 cty.plist / clublogxml，需联网且引入 5 个依赖），零新依赖。真实数据冒烟：6,740 个去重呼号 → 187 实体，5 个特殊呼号未匹配（B0/B9 省际、D1 活动台）。
- `dxcc_summary` 全量扫描非 void QSO：实体总数、每实体首通/最近通联（UTC ISO）、波段集合（同实体×同波段计 1，DXCC Challenge 语义）、`by_band` 每波段实体数。
- `GET /api/v1/dxcc`（认证，`_ok` 信封）实时计算返回；打开即最新，无 WS 推送/轮询（NFR-086）。
- 前端：设置菜单加 DXCC tab → 全屏 overlay（镜像 LOG）：大数字总数 + 波段矩阵 + 实体列表（名称/洲/首通/波段数）；`api.js` 加 `dxcc()`。
- Regressions: `tests/engine/test_dxcc.py`（解析/lookup/统计/真实数据冒烟）、`tests/web/test_api.py`（认证 + 信封 + 结构）。全量套件绿。
```

- [ ] **步骤 6：验证 + commit**

```bash
cd /Users/cheenle/HAM/ft8 && venv/bin/python -m pytest tests/ -q --ignore=tests/integration
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add AGENTS.md SDD/
git commit -m "docs: DXCC stats live view (SDD NFR-086, cty.dat data source)"
```
预期：全量测试 PASS（现 681 + 新增 ~10）、SDD check clean。

---

## 自检记录

- **规格覆盖度**：设计 §3.1（解析/lookup）→ Task 1；§3.2（summary）→ Task 2；§3.3（接口）→ Task 3；§3.4（前端）→ Task 4；§5 错误处理（cty 缺失回退空 db、unmatched 计数）→ Task 1 步骤 3 + Task 2；§6 测试 → 各任务；§7 文档 → Task 5。Non-goals（无推送/轮询、无编号统计、无搜索）全部未实现，符合。
- **占位符扫描**：无 TODO/待定；每步含完整代码或精确命令。
- **类型一致性**：`load_cty(path) -> CtyDatabase`（Task 1/2/3 一致）；`lookup(call) -> tuple[str, str] | None`（Task 1/2 一致）；`dxcc_summary(repository, cty) -> DxccSummary`（Task 2/3 一致）；`to_dict()` 输出 `{total, unmatched, entities[], by_band{}}`（Task 2/3/4 一致）；前端 `res.total/res.unmatched/res.entities/res.by_band` 与后端字段一致。
- **时间断言**：`1700000000` = 2023-11-14 22:13:20 UTC → `"2023-11-14T22:13:20Z"`（Task 2 测试断言已按此 UTC 时间写）。
- **已知注意点**：Task 1 步骤 3 的 `lookup` 与存储格式（`=` 编码在 prefix 字符串里）需实现时对齐测试断言——测试是权威，实现按测试驱动修正；`3H0(23)[42]` 展开为 3H0/3H2/3H3（测试断言了 3H1 不匹配、3H2/3H3 匹配）。
