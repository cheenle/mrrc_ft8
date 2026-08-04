# MRRC-FT8 DXCC 统计 + 菜单实时展示 — Design

**Date:** 2026-08-04
**Status:** Draft for review
**Scope:** 基于 canonical store 的 QSO 数据实时统计 DXCC 实体，在设置菜单加 "DXCC" 入口，全屏 overlay 展示总数 + 实体列表 + 波段矩阵。

## 1. Purpose

操作员的 canonical store 现有 10,256 条 QSO（含 JTDX 历史导入），去重后 6,740 个呼号。需求：统计通联过的 DXCC 实体（国家/实体，如 China、Japan、Mauritius…），并在 Web 菜单里实时展示——打开即最新，不做常驻推送。

Confirmed decisions (brainstorm, 2026-08-04):
- 统计内容：**B — DXCC 总数 + 实体列表 + 波段矩阵**。
- 实时性：**A — 打开 DXCC 页时后端实时从数据库计算**（无 WS 推送、无轮询）。
- 实体解析：**自写 `cty.dat` 解析器**（已验证）。原方案 A（pyhamtools 0.13.0）经可行性验证被否决：其 `LookupLib` 的 `countryfile` 需要 Country-files 的 `cty.plist`（XML plist）而非本仓库的 `cty.dat`（ADIF 文本格式），clublogxml 需每日联网下载，且引入 bs4/ephem/lxml/redis/requests 五个依赖——对离线轻量服务器不合适。
- 数据源：仓库根目录 `cty.dat`（4,172 行，country-files 旧版 ADIF 格式：`name: cqz: ituz: cont: lat: lon: tz: prefix:`，**无 DXCC 编号列**——实体身份即国家名，足够统计）。解析冒烟验证：6,740 呼号 → 187 实体，仅 5 个特殊呼号未匹配（B0/B9 省际前缀、D1 活动台，0.07%）。

## 2. Current Structure

- `server/engine/repository.py` — `Repository.list_qsos()`（可带 `since_days`）、`qso` 表含 `dx_call/band/completed_epoch/status`。
- `server/web/api.py` — `create_router` 下所有 REST 带 `_ok` 信封；`/logs/qsos`、`/logs/adif` 已限 7 天窗口（NFR-085）。
- `server/web/static/index.html` — 设置抽屉 tabs：Radio/FT8/Station/Log；Log 走全屏 overlay（`#log-overlay`）。
- `server/web/static/js/settings.js` — `switchTab("log")` → `openLogView()` 模式（overlay + `api.qsos()` + 渲染）。

## 3. Design

### 3.1 `server/engine/dxcc.py`（新建，纯函数零依赖）

- `@dataclass CtyEntity`: `name: str`、`continent: str`、`prefixes: list[str]`（解析后的匹配模式，含展开结果）。
- `load_cty(path) -> CtyDatabase`：解析 `cty.dat`：
  - 实体行正则：`^(.*?):\s*\d+:\s*\d+:\s*(\S+):\s*[-\d.]+:\s*[-\d.]+:\s*[-\d.]+:\s*(\S+):\s*$`（name/continent/主前缀；CQZ/ITUZ/坐标/tz 忽略）。
  - 前缀列表跨行收集（续行前导空格，逗号分隔，`;` 结束）。
  - 条目展开：`=X` 精确匹配；`X(23)[42]` → `X0/X2/X3` 三个数字替换前缀（`[42]` 编号覆盖忽略——本数据源无编号列，且展开规则只需数字位替换）；普通 `X` 前缀匹配。
- `CtyDatabase.lookup(call: str) -> tuple[str, str] | None`：`base = call.split("/")[0].upper()`；先精确匹配（`=`），再**最长前缀**匹配（含数字替换展开结果）；返回 `(entity_name, continent)`。
- 匹配顺序保证确定性：精确 > 前缀（同长按实体行序，前者优先）；结果为 None 时上层把该 QSO 计入 `unmatched` 统计但不断言失败。

### 3.2 `server/engine/dxcc_stats.py`（并入 dxcc.py 或独立小模块）

- `dxcc_summary(repository, cty, *, clock=time.time) -> DxccSummary`：
  - `@dataclass DxccSummary`: `total: int`、`unmatched: int`、`entities: list[DxccEntityStat]`、`by_band: dict[str, int]`。
  - `@dataclass DxccEntityStat`: `name`、`continent`、`first_utc`（ISO 字符串）、`last_utc`、`band_count`、`bands`（list[str]）。
  - 逻辑：遍历 `repository.list_qsos()`（非 void 全量，历史 + live）→ `cty.lookup(dx_call)`；命中则更新该实体的 `first_utc = min(...)`、`last_utc = max(...)`、bands 集合、`by_band[band] += 1`（每实体每波段计 1，同实体同波段多次通联不重复计——DXCC Challenge 语义）；未命中计入 `unmatched`。
  - 呼号→实体结果用 dict 缓存（QSO 里同呼号重复出现，6,740 去重呼号只查一次）。
  - `entities` 按 `name` 排序；`first_utc` 由 `completed_epoch`（UTC）格式化。
  - 复杂度：~1 万行遍历 + 最长前缀匹配（346 实体），实测毫秒级；打开时实时计算，不缓存（数据低频变化，QSO 完成才变）。

### 3.3 后端接口 `server/web/api.py`

```python
@router.get("/dxcc")
async def dxcc(session: Session = Depends(require_session)) -> JSONResponse:
    summary = await asyncio.to_thread(
        dxcc_summary, state.repository, _cty_database()
    )
    return _ok(summary.to_dict())
```

- `_cty_database()`：模块级懒加载单例（`functools.lru_cache` 或模块全局 + `None` 初始化），路径固定 `repo_root/cty.dat`（与 `_static_dir()` 同源定位方式：`Path(__file__).resolve().parent.parent / "cty.dat"`）。
- 返回 `{"ok": true, "total": N, "unmatched": M, "entities": [...], "by_band": {...}}`。

### 3.4 前端

- `server/web/static/index.html`：
  - 抽屉 tabs 加 `<button class="tab" data-tab="dxcc">DXCC</button>`（Log 之后）。
  - 加全屏 overlay（复用 log-overlay 的类与样式）：`#dxcc-overlay` + `#dxcc-content` + `#btn-dxcc-close`。
- `server/web/static/js/settings.js`：
  - `switchTab` 加 `if (tab === "dxcc") { close(); openDxccView(); return; }`。
  - `openDxccView()`（镜像 `openLogView`）：显示 overlay → `await api.dxcc()` → 渲染：
    - 头部大数字：`<strong>${total}</strong> DXCC`（未匹配 `+${unmatched} unmatched` 可选小字）。
    - 波段矩阵：`by_band` 排序渲染为表行 `20m · 120`。
    - 实体列表：`<div class="qso-row"><span class="qso-call">${name}</span><span class="qso-meta">${continent} · ${first_utc} · ${band_count} bands</span></div>`（复用 LOG 行样式）。
    - 失败分支显示错误文本（同 LOG）。
  - `api.js` 加 `dxcc: () => request("/dxcc")`。
- 样式复用 `.log-overlay/.log-panel/.log-list/.qso-row`，无需新 CSS（或仅加 `.dxcc-total` 大数字样式）。

### 3.5 Non-goals

- 不做 WS 常驻推送 / 轮询（打开即最新，决策 A）。
- 不做 DXCC 编号（本数据源无编号列；实体名即统计口径）、不做 CQ/ITU 分区统计。
- 不做按波段筛选/搜索（YAGNI）。
- 不修改 `cty.dat` 数据本身；`unmatched` 呼号（B0/B9 省际、D1 活动台）如实计入计数但不断言。

## 4. Data Flow

菜单 DXCC 点击 → 关抽屉 → `#dxcc-overlay` 显示 → `api.dxcc()`（`GET /api/v1/dxcc`，session cookie）→ 后端 `to_thread(dxcc_summary)`（全量 QSO → cty 查表 → 实体统计）→ `_ok` JSON → 前端渲染总数/矩阵/列表。关闭按钮或背景点击隐藏；不影响 rig/sequencer/lease。

## 5. Error Handling

- `cty.dat` 缺失/解析失败：模块加载时 `log.exception` 并回退空 `CtyDatabase`（所有呼号 unmatched），接口仍返回 200 + `total=0`，前端显示空态；不 fault safety。
- `lookup` 未命中：计入 `unmatched`，不影响其余统计。
- DB 读取失败：`to_thread` 内抛错 → 500（repository 已有 DBMOVED 自愈，罕见）；前端显示错误文本。

## 6. Testing

- `tests/engine/test_dxcc.py`（新）：
  - `load_cty` 解析（实体数、续行收集、`=`精确条目、`(23)`数字替换展开、跨行前缀）。
  - `lookup`：普通前缀（BI1TX→China）、精确匹配（`=3D5X` 类）、数字替换（`3H0(23)[42]` 展开匹配）、斜杠后缀剥离（`B0/BD7OXR` → base `B0`）、未命中返回 None。
  - `dxcc_summary`：用 fixture repository（造 3-4 条 QSO 覆盖同实体多波段/多呼号/未匹配）断言 total、by_band 语义（同实体同波段不重复计）、first/last_utc、排序。
  - 真实 `cty.dat` + 真实 QSO 冒烟：断言 total == 187、unmatched == 5（数据变动时以实际为准，total ≥ 180）。
- `tests/web/test_api.py`：`/api/v1/dxcc` 未认证 401；认证后 200 + `ok:true` + `total` int + `entities` 结构。
- 文档：AGENTS.md 模块表、SDD/05（NFR：DXCC 打开即最新）、SDD/11 组件表、SDD/12（cty.dat 数据源）、SDD/14 版本历史。

## 7. Deployment / Docs

- 无新 Python 依赖（自写解析器）。
- `cty.dat` 随仓库携带（已在根目录）；生产部署路径与仓库根一致。
- 前端无需构建步骤（vanilla JS）。
