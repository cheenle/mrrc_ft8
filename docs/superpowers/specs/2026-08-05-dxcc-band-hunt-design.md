# MRRC-FT8 传播驱动的新 DXCC 波段猎人 — Design

**Date:** 2026-08-05
**Status:** Draft for review
**Scope:** 通过另一代码库（pskreporter）的 StarRocks 数据库探测"哪个 FT8 波段有新 DXCC 实体、且传播开通到本网格"，空闲时自动把 FT8 服务器切到该波段，侦听到新 DXCC 后由现有自动呼叫链路发起通联。两库严格隔离，仅经 HTTP 接口协作。

## 1. Purpose

FT8 服务器一次只守一个波段（7/14/21/28 MHz）。当前"新 DXCC 自动呼叫"（NFR-087）只在**当前波段**内等新 DXCC 出现；如果传播开通的是别的波段，它看不到。本设计让服务器**自主换波段去守**：借助 pskreporter 仓库每分钟级抓取的全球传播数据（`all_records`），选出"附近网格此刻能听到新 DXCC"的波段，空闲时把 rig 切过去，再靠已有自动呼叫闭环。

Confirmed decisions (brainstorm, 2026-08-05):
- 集成方式：**B — pskreporter 出 `GET /api/band_hunt` 端点，FT8 薄轮询**。两库隔离：互不 import、不共享 DB 凭据；唯一接口是 localhost HTTP。
- 传播判据（用户强调）：**候选波段必须在附近网格（ON80DA 周边 ~1000 km）有活跃接收记录** —— 即使全球某处有新 DXCC 在 CQ，只有本区域能"听到"该波段才切。
- 波段范围：**扩展全部 FT8 波段** 7/10/14/18/21/24/28 MHz（FT8 呼号频点）。
- 自动化程度：**全自动无人工**。空闲时自动切频 → 侦听到后自动通联。
- 新鲜度：**5 分钟级已天然满足**（`all_records` 本就是 `*/5` 增量抓取），无需再动 pskreporter 调度。
- FT8 侧双闸门：启动配置（env）决定功能是否加载；设置菜单栏一个开关 `auto_band_hunt` 运行时控制。

## 2. 隔离边界（本次设计的第一约束）

```
                    ┌──────────────────────────────┐
                    │  pskreporter 仓库            │
                    │  StarRocks all_records (5min)│
                    │  web_app :5000               │
                    │  grid_to_latlon /            │
                    │  compute_distance_km /       │
                    │  dxcc_lookup / band_utils    │
                    └───────────▲──────────────────┘
                         GET /api/band_hunt
                    ┌───────────┴──────────────────┐
                    │  mrrc-ft8 仓库               │
                    │  band_hunter.py (薄轮询器)   │
                    │  现有 rig 波段切换路径       │
                    │  现有 auto-call (NFR-087)    │
                    └──────────────────────────────┘
```

- **pskreporter** 拥有全部传播/距离/DXCC/波段逻辑，mrrc-ft8 不复制这些。
- **mrrc-ft8** 只发一个 HTTP GET、解析 JSON、用**自己**的 worked 实体集过滤、用**自己**的 rig 路径切频。不 import pskreporter 任何模块，不持有 StarRocks 凭据。
- 接口契约（下述 §3.1）即两库之间唯一耦合点，任何一侧可独立演进。

## 3. Design

### 3.1 pskreporter 端点：`GET /api/band_hunt`

Query `all_records`（`mode='FT8'`）近 `window_min` 分钟：

1. **传播门**：仅保留 `receiver_locator` 距 `home_grid`（ON80DA）`≤ radius_km` 的 spot（用现有 `grid_to_latlon` + `compute_distance_km`）。
2. 按波段聚合（复用 `band_utils.get_band_from_frequency`）；**nearby_spot_count ≥ 阈值**（默认 5）的波段才进入候选。
3. 每波段列出：去重呼号集合、DXCC 实体（`dxcc_lookup`）、平均 SNR、最近 spot 时间。
4. **不做 worked 过滤** —— worked 权威在 FT8 侧。

请求：
```
GET /api/band_hunt?home_grid=ON80DA&radius_km=1000&window_min=30
```

响应（成功 `_ok` 风格一致）：
```json
{
  "ok": true,
  "generated_at": "2026-08-05T02:00:00Z",
  "home_grid": "ON80DA",
  "bands": [
    {
      "band": "20m",
      "dial_freq_hz": 14074000,
      "nearby_spot_count": 483,
      "distinct_calls": ["VR2BG", "JN3GYJ", "RA0SCA"],
      "entities": [{"name": "Hong Kong", "adif": 321}, {"name": "Japan", "adif": 339}],
      "avg_snr": -8.5,
      "most_recent_ts": "2026-08-05T01:59:00Z"
    }
  ]
}
```

错误：`{"ok": false, "reason": "..."}`（DB 不可达 / 无数据）。实现位置：`web_app.py` 新路由 + 复用现有 DB helper 与工具函数；`dial_freq_hz` 从 `band_utils` 的 FT8 呼号频点表取。

### 3.2 mrrc-ft8：`server/engine/band_hunter.py`

纯函数 + 一个 async 轮询任务，保持与 auto-call 一致的"系统任务、免租约"模式：

- `fetch_opportunities(base_url, home_grid, radius_km, window_min)` — `httpx` GET，超时（如 5 s），返回解析后的 JSON；失败/非 200 → 返回 `None`（本轮跳过）。
- `rank_bands(opportunities, worked_entities)` — **纯函数**：对每个候选波段，用本地 `worked_entities`（dxcc cache 实体名集合）剔除已通联实体；只保留有"新 DXCC 实体"的波段；排序键 `(新实体数 desc, nearby_spot_count desc, avg_snr desc)`；返回带 `new_entities` 的排行。
- `decide_switch(ranked, current_band, idle, cooldown)` — **纯函数**：非 idle → 不切；榜首波段 == 当前波段 → 不切；距上次切频 < `cooldown` → 不切；无候选 → 不切；否则返回目标波段。
- `run_hunt_once(...)` — 编排：fetch → rank → decide → 若需切换则走 rig 切频（见 §3.4）。

### 3.3 配置与开关（双闸门）

- **启动配置**：`ServerConfig` 增加 `band_hunt_url`（env `MRRC_FT8_BAND_HUNT_URL`，**默认空 = 功能整体不加载**，不启动轮询任务、不影响现有行为）。可配 `MRRC_FT8_BAND_HUNT_RADIUS_KM`（默认 1000）、`MRRC_FT8_BAND_HUNT_WINDOW_MIN`（默认 30）、`MRRC_FT8_BAND_HUNT_INTERVAL`（默认 300 s）、`MRRC_FT8_BAND_HUNT_COOLDOWN`（默认 1200 s = 20 分钟驻留）。
- **运行时开关**：`SETTING_SCHEMA` 加 `auto_band_hunt`（bool，默认 False）。FT8 设置菜单栏加一个 toggle（同 `auto_call_new_dxcc` 的后端持久化 + localStorage 回显模式）。
- **联动**：猎取到目标波段并切换后，实际发起通联复用 `auto_call_new_dxcc` 链路 —— 即只有 `auto_band_hunt` **和** `auto_call_new_dxcc` 同时开启才构成完整无人值守闭环；仅开 `auto_band_hunt` = 只自动切频不自动呼叫。

### 3.4 切频路径（复用现有，非新增串口/rig 逻辑）

- 复用 `/radio/band` 内部同一套 rig 调频原语（`rig.set_frequency` + 必要校验），但由系统任务调用，**不走 HTTP 租约层** —— 与 auto-call 如何绕过租约的模式一致（NFR-087）。
- 切换时机：orchestrator 空闲（sequencer 非 REPLYING/QSO 中、`safety` 未 armed、无 CQ loop 持有）。若 rig busy / TX armed → 本轮跳过，下一轮再试。
- 每次切换 audit 一行（`operation='band_hunt'`，detail 目标波段 + 依据：新实体数/邻近 spot 数）。

### 3.5 波段覆盖扩展

- `server/web/static/js/band.js`：`FT8_BANDS` 扩为 7 项 —— 7.074 / 10.136 / 14.074 / 18.104 / 21.074 / 24.915 / 28.074 MHz。
- `/radio/band` 校验：放开到上述 7 个频点（或"落在任一 FT8 波段容差内"）。
- 目标波段与 `band_hunt` 的 `dial_freq_hz` 对齐：切频用该频点。

### 3.6 状态可见性

- state snapshot 可选暴露 `band_hunt: {enabled, current_rank, next_target, last_switch_ts}`，供驾驶舱/日志观察"猎手在做什么"，不阻塞核心路径。

## 4. 错误处理

- 端点超时/5xx/`ok:false` → 本轮跳过，保持当前波段，DEBUG 日志，绝不因 DB 侧故障干扰接收/发射。
- 无新 DXCC 候选 → 不切频（留在原波段），避免抖动。
- 冷却未满 → 不切频。
- 轮询任务本身异常 → 单次 try/except，任务不死亡（同 hourly maintenance 的模式）。
- 从不影响：PTT 安全、sequencer、当前 QSO。

## 5. 测试

**pskreporter 仓库**
- `band_hunt` 评分逻辑单测（mock 行：传播门过滤、波段聚合、附近 spot 阈值、实体映射）。
- 端点测试：`ok` 信封、空数据、`ok:false`（DB 异常路径）。
- 现有测试不回归。

**mrrc-ft8 仓库**
- `band_hunter`：`rank_bands`（worked 过滤、排序）、`decide_switch`（空闲/忙碌/冷却/同波段/无候选矩阵）、`fetch_opportunities`（mock HTTP：200 / 超时 / 非 ok）。
- 设置：`auto_band_hunt` round-trip（GET/PUT /settings）+ 422。
- 启动配置：`MRRC_FT8_BAND_HUNT_URL` 未设时不启动轮询任务（现有行为不变）；设置时启动。
- 波段表扩展：band.js 静态契约 + `/radio/band` 校验新增频点。
- 全量套件绿。

## 6. Out of scope

- 不动 pskreporter 的抓取调度（5 分钟已满足）。
- 不新增 rig 串口/新硬件路径；一切经现有 rigctld。
- 不做"指定呼号猎取"——只做"波段级机会"（新 DXCC 实体由服务器实时解码捕获）。
- BG1SB 专属 `receiver_records` feed 恢复不在本设计范围（当前用 `all_records` 邻近接收台代理）。
