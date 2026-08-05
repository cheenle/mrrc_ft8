# MRRC-FT8 new-DXCC hunt 反应速度优化 — Design

**Date:** 2026-08-05
**Status:** Draft for review
**Scope:** 消除 new-DXCC hunt 链路中实测到的慢点：去掉 3/7 天深窗口、把 `CtyDatabase.lookup` 索引化（保留 cty.dat 完整匹配语义）、让 `dxcc_summary` 重建从 10 s 降到 ~0.2 s、修复自动波段猎人的陈旧 worked 集、并给 dashboard 加渐进渲染 + 上游 TTL 缓存。

## 1. Purpose

用户报告 newDXCC hunt 反应慢。对运行中的服务器实测（2026-08-05），基线数据：

| 环节 | 实测耗时 | 位置 |
|---|---|---|
| `cty.lookup(call)` 单次 | **1.5 ms** | `server/engine/dxcc.py:55`，线性扫描 346 实体 × ~38.5k 前缀 |
| `dxcc_summary` 全量重建 | **10.2 s** | `dxcc.py:164`；DB 10,533 QSO → 6,749 次 lookup × 2.59 亿 `str.startswith` |
| `/dxcc`、`/band-hunt` 首次请求 | 10 s+ | `api.py:707/759` 缓存 dirty 时同步重建 |
| 上游 `/api/band_hunt` | 冷 2.1 s / 热 <1 ms | 深窗口（1/3/7 天）冷缓存 5-8 s |
| dashboard `Promise.all` 全等 7 窗口 | 被最慢窗口阻塞 | `settings.js:413` |
| 自动波段猎人轮询粒度 | 60 s | `.env` `MRRC_FT8_BAND_HUNT_INTERVAL=60` |
| 解码 → is_new_dxcc 标记 | 14.8 ms/时隙 | 事件循环上，**不慢** |
| `rank_bands` / `get_setting` | <10 µs / 5 µs | **不慢** |

主要根因：`lookup()` 的暴力全扫 + 每次 QSO 写入后全量重建 + dashboard 等待深窗口。

Confirmed decisions (brainstorm, 2026-08-05):
- "不只比长度" = **保留 cty.dat 完整匹配语义**：`=CALL` 精确优先 → 最长前缀 → `(数字)` 替换展开，索引实现必须逐字节等价。
- dxcc_summary：**索引加速 + 保留重建**，不做增量维护（增量对 `void_qso` 撤销难做对）。
- 自动波段猎人的陈旧 worked 集：**一起修**。

## 2. Design

### 2.1 去掉 3/7 天深窗口

`server/web/static/js/settings.js:394` 的 `BAND_HUNT_WINDOWS` 从 7 项砍到 5 项：

```js
const BAND_HUNT_WINDOWS = [
  [10, "10 min"], [30, "30 min"], [60, "1 hour"],
  [240, "4 hours"], [1440, "1 day"],
];
```

- 移除 `[4320, "3 days"]` 和 `[10080, "7 days"]`。
- 保留 1 天作为最深窗口。
- 由于 `Promise.all`（`settings.js:413`）等最慢窗口，去掉 3/7 天后面板渲染等待上界从 ~8 s 降到 1 天窗口冷缓存耗时；再配合 §2.4 的渐进渲染，小窗口立即显示。
- 同步更新 `SDD/14-version-history.md`（87b68e1 深窗口记录）与 `SDD/` 相关章节。

### 2.2 索引化 `CtyDatabase.lookup()` — 保留完整匹配语义

公开接口与数据结构不变（`entities: list[CtyEntity]`、`lookup(call) -> tuple[str, str] | None`），只在加载后构建附加索引。

**现状语义**（`dxcc.py:55`）：
1. `base = call.split("/", 1)[0].upper()`（去 `/后缀`、大写）。
2. 精确匹配 `=CALL`：`base == stored[1:]` → **立即返回**（精确永远优先）。
3. 否则最长前缀命中；等长并列保留**最先遇到**的实体。
4. 均不中 → `None`。

**索引**：
- `self._exact: dict[str, tuple[str, str]]`：所有 `=pattern` 展开后键 → `(name, continent)`。构建用 `setdefault`（保留最先遇到，与遍历顺序一致）。
- `self._trie: dict`：嵌套字典前缀树，存所有非 `=` 展开前缀。每个完整前缀节点挂 `(name, continent)`；同样用 setdefault 语义挂实体（等长并列最先遇到）。

**lookup 流程**：
1. `base = call.split("/", 1)[0].upper()`。
2. `self._exact.get(base)` 命中 → 返回（精确优先）。
3. 沿 `_trie` 走 `base` 每个字符，记录**最深**的挂实体节点 → 返回该实体。
4. 走到底无实体 → `None`。

**性能**：单次 lookup 从 ~38.5k 次 `startswith` → O(len(base)) 字典查表（5-6 步）。1.5 ms → 亚微秒。`dxcc_summary` 重建从 10.2 s → ~0.2 s（`list_qsos` 0.065 s + 索引 lookup + 排序）。`on_decode` 每时隙 14.8 ms → ~1 ms 级。

**语义保真守门测试**（§5）：把旧线性实现（保留一份纯函数参考）与新索引实现跑在同一语料上，断言结果完全一致。语料：
- 真实 QSO 日志全部 `dx_call`（10,533 个）。
- cty.dat 全部展开模式 + 各加后缀/前缀扰动。
- 合成呼号：`/后缀`、`(数字)` 替换组合、`=CALL` 精确、未匹配、边界（空/极短/大写小写混合）。

### 2.3 dxcc_summary 重建保留 + 修 band-hunt 陈旧 worked 集

- **重建机制不变**：QSO 写入/导入/作废置 `dxcc_dirty=True`（`repository.py:235/274/380`），下次 `/dxcc` 或 `/band-hunt` 重建。重建已降到 ~0.2 s，无需增量维护。
- **抽共享刷新函数**：把 `api.py:707-713` 与 `main.py:603-609` 已有的"dirty 时重建缓存"逻辑抽成 `server/web/api.py` 内的 `_fresh_dxcc_cache(state)`（与 `_cty_database()` 相邻；`main.py` 已 import web.api 的 `_snapshot`，再 import 此函数不引入新分层），两处复用，避免漂移。
- **修陈旧集**：`band_hunt_loop`（`main.py:716`）每 tick 先调 `_fresh_dxcc_cache(state)`，`dxcc_dirty` 时重建后再取 `worked` 集。刚通联的实体不再被当作"新 DXCC"重复 hunt。

### 2.4 反应链路：渐进渲染 + 上游 TTL 缓存

针对剩余最慢环节（1 天窗口冷缓存 5-8 s、多窗口串/并行全等）：

- **渐进渲染**：`settings.js:413` 从 `Promise.all`（等最慢）改为逐窗口 `fetch` 后**先到先渲染**。10 分钟窗口 ~1 s 内可见，1 天窗口后续补上。渲染函数拆成 `renderWindow(res, label)` 单窗口版本。
- **上游 TTL 缓存**：`api.py` `band_hunt_proxy` 加进程内缓存，键 `(window_min, home_grid, radius_km)`，值 `(epoch, body)`。TTL = `min(window_min, 3600)` 秒：小窗口短 TTL 保新鲜，深窗口长 TTL 免重复冷拉。band_hunt_loop 的 30 分钟轮询与 dashboard 多次打开共享热缓存。
  - 边界：`detail=1` 请求与 `detail=0` 分开缓存（响应体积不同）；错误响应（非 200/`ok:false`）不缓存。
  - 并发：首个请求在途时后续请求直接等待同一 in-flight future（可选，`asyncio` 单事件循环下简单）。

## 3. 错误处理

- 上游超时/5xx/`ok:false` → 代理返回 502，不缓存，保持现状。
- 缓存满：TTL 自然过期 + 键数量上限（如 64）LRU 驱逐，防止深窗口枚举撑爆内存。
- band_hunt_loop dirty 刷新失败 → 用旧缓存，本轮跳过（绝不让 worked 集刷新失败影响切频决策）。

## 4. 兼容性

- `CtyDatabase` 公开接口不变，`get_cty_database()` 单例不变，`entities` 字段不变（`dxcc_summary`、worked 集构建、`band_hunt_proxy` 过滤均沿用）。
- 前端 `BAND_HUNT_WINDOWS` 只缩窗口列表，渲染逻辑兼容。
- 环境变量全部不变（轮询间隔保持 60 s，用户未要求改粒度）。

## 5. 测试

**`tests/engine/test_dxcc.py`**
- 语义一致性回归：旧线性实现 vs 新索引实现，跑真实日志语料 + 合成语料，逐 call 断言相等。
- 索引单测：精确优先、最长前缀、等长并列最先、`(23)` 数字替换、`/后缀` 剥离、未匹配 `None`、空/边界输入。
- `dxcc_summary` 结果不变性（新实现跑同 DB 数据，统计一致）。

**`tests/engine/test_band_hunter.py`**
- `band_hunt_loop` 触发 dirty 刷新：模拟 `dxcc_dirty=True` → 刷新后用新 worked 集，不再返回刚通联实体。

**`tests/web/`**
- 上游 TTL 缓存：mock httpx，断言同键二次请求命中缓存（upstream 只被调一次）、TTL 过期后重新拉、`detail` 分开缓存、错误不缓存。
- 现有 API/前端测试不回归。

**全量套件**：`venv/bin/python -m pytest tests/` 绿。

## 6. Out of scope

- 不改自动波段猎人轮询粒度（60 s，环境变量可调，用户未要求）。
- 不做 dxcc_summary 增量维护。
- 不改 cty.dat / ADIF 解析格式。
- 不动 pskreporter 侧端点。
