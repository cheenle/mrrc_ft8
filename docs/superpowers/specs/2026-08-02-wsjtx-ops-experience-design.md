# WSJT-X 操作体验对齐 —— 自动 CQ 循环 + Band Activity 解码表

- 日期： 2026-08-02
- 状态： 已批准（用户逐节确认 §1/§2/§3)
- SDD 引用： AD-010（租约）、AD-011（横屏驾驶舱）、§10.1–10.3(REST/WS 契约）、§11.3（组合根）、第 15 章（PTT 安全）、NFR-055（重发预算）、§15.4(dead-man)

## 背景与目标

MRRC-FT8 的 Web 驾驶舱当前是最小操作面：Take control → 选台 Reply / CQ → sequencer 自动走完 Tx1–Tx5 并记日志。用户要求向 WSJT-X 操作体验对齐，经澄清锁定两个增量：

1. **自动 CQ 循环**：点一次 CQ 后，无人应答继续呼；有人应答自动走完 QSO;QSO 完成或失败后自动重新 CQ;连续 N 分钟无 QSO 自动停；人随时可接管/停止。
2. **Band Activity 式解码表**：右侧候选栏从纯文本列表升级为等宽紧凑多列（UTC/dB/dt/freq/报文），支持双击直接应答，CQ 与指向本台的报文视觉区分。

明确不做：双窗格布局、桌面端重做、报文手工编辑、瀑布点选 Tx/Rx 频点、无人值守全自动应答、时隙分隔线、Erase 按钮、列表排序选项。

## 前置发现（2026-08-02 计划阶段补记）

核查生产链路确认：当前**没有 TX 驱动**——`sequencer.next_tx_message()` 与 `safety.transmit()` 在生产代码中均无调用者（orchestrator 预留 `on_slot_start` 钩子未接线）,`pop_log_record()` 的 QSO 落库也未接线。即现状 CQ/Reply 只布防不走发射。因此本 spec 增加前置任务链，作为 CQ 循环与操作体验的基础：

1. **TX 驱动**(`server/engine/tx_driver.py`)：挂 orchestrator `on_slot_start`,FT8 收发交替（默认偶数时隙发、奇数时隙收），每合格时隙取一次 `next_tx_message()` → 经 Worker 编码（Protocol v1 `encode` + 复用一段 606,720×4 字节 TX 共享内存，新 `server/engine/dsp_encode.py` 的 `SupervisorEncoder`，与 `SupervisorDecoder` 同模式）→ `safety.transmit()`。PTT 仍只在 safety 边界。
2. **QSO 落库接线**:1 秒节奏轮询 `pop_log_record()` → `repository.record_qso()`（现有 30 秒撤销窗口语义不变）。

发射失败由 safety 故障矩阵处理（disarm/fault),TX 驱动只计数不重试；编码/播放错误不绕过 `TxRefused`。

## 设计前提（不可妥协的安全不变量）

- TX 仍只经 safety 布防路径；循环控制器不产生任何新的 PTT 通路。
- 任何已认证会话的 STOP 保持最高优先、幂等、立即生效。
- 循环只在控制租约有效时运行；租约过期/断线（dead-man 15 秒）立即停止循环，不僵死、不复活。
- 故障（§15.5 故障矩阵）不自动重布防；循环遇到布防失败即终止。

## §1 自动 CQ 循环（服务端）

### 组件

新模块 `server/engine/cq_loop.py`，类 `CqLoopController`，包住现有 `Sequencer` 而不修改其状态机。职责单一：根据 sequencer 状态迁移决定"续呼 / 终止"，并执行空闲超时。

依赖（全部注入，保持硬件无关可测）:

- `Sequencer` 实例（读状态、调 `start_cq()` / `stop()`)
- 布防回调（与现有 API CQ 通路相同的 safety arm 入口，循环复用它，不新建通路）
- 租约状态源（lease 事件回调：release/expire/disconnect)
- 时钟与 sleeper（与 orchestrator 相同的注入风格）
- 设置读取回调（`cq_loop_idle_timeout_s`，每 tick 重读）
- 审计回调（`cq_loop_start` / `cq_loop_stop`,detail 带原因）

### 续呼规则

| sequencer 迁移 | 循环行为 |
|---|---|
| `DONE`(QSO 完成、已记日志） | 重新 CQ；空闲计时重置 |
| disarm `EXHAUSTED`（重试预算耗尽， NFR-055) | 视为失败 QSO，重新 CQ |
| disarm `PARTNER_LOST`（对方呼别人， 反 QRM) | 视为失败 QSO，重新 CQ |
| disarm `MANUAL` / STOP / 任何故障类 | 终止循环，不续呼 |

### 空闲超时

- 复用 1 秒 watchdog 节奏（与 lease watchdog 同模式）。
- 计时在每次 `DONE` 重置；连续超过 `idle_timeout_s`（默认 600，范围 60–3600）无 QSO → 停止循环并 disarm（原因 `timeout` 进审计）。
- 明确：失败 QSO(`EXHAUSTED` / `PARTNER_LOST`）续呼但**不重置**计时——超时语义是"连续无完成的 QSO"，防止长期空呼/近失循环。
- 设置运行中修改即时生效（每 tick 重读）。

### 租约门控

- 启动循环要求调用方持有租约（API 层强制，同现有 CQ)。
- 租约 release/expire/disconnect 事件 → 立即停止循环（dead-man 的 priority STOP 由现有接线触发，控制器只负责同步终止循环状态）。

### 状态与审计

- state 流快照 `sequencer` 对象增加 `cq_loop: {active: bool, idle_remaining_s: int}`;revision 正常递增。
- 审计事件：`cq_loop_start`（带 idle_timeout_s)、`cq_loop_stop`(detail: `timeout` / `manual` / `lease_lost` / `fault` / `arm_refused`)。

## §2 Band Activity 解码表（前端 + 协议补字段）

### 协议（纯增量，向后兼容）

`server/main.py` 的 `on_decode` 批次 messages 每项增加：

- `dt: float`、`freq: float` —— 直接取自 `DecodeResult`;
- `to_me: bool` —— 报文指向本台，由 `msgparse` 解析结果在服务端判定（不用前端字符串匹配）。

WS 发送侧 `{"type": "decodes", **batch}` 不变；旧客户端忽略新字段。

### 前端渲染（`server/web/static/js/candidates.js` 重写渲染，布局不动）

- 保持 320px 右栏与横屏 grid(AD-011 不动）;`index.html` 结构不变（仍为 `<ul id="candidate-list">`)，只改行内渲染，无内联 JS。
- 行格式（等宽）:`16:21:00 -12 +0.2 1234 CQ BI6PWL OM64`(UTC、SNR、dt、freq Hz、报文），新时隙行插顶部，保留 200 条上限；`state.js` 的 slot+text 去重不变。
- 视觉区分：CQ 行加粗；`to_me` 行高亮底色；`late` 批次行降透明度。
- 交互：单击 = 选中并点亮 Reply（现状）;**双击/双触 = 直接 Reply**，调用与按钮完全相同的 `api.reply` 通路，不绕过布防/租约校验。选中行样式保留。
- CSS 只新增行/高亮/加粗几个类；底栏 sequencer 区显示 `CQ LOOP MM:SS` 倒计时，循环停止后回到 `idle`。
- 完成后 service worker 缓存名按惯例 bump(v7),static 契约测试同步。

## §3 API、设置、错误处理、测试

### REST（lease-gated + Idempotency-Key，沿用现有契约）

- `POST /api/v1/operation/cq` 请求体增加可选字段：`loop: bool`（缺省 false)、`idle_timeout_s: int`（缺省读设置项）。
- 不带 `loop` 的行为与现状完全一致；循环期间重复发 CQ 幂等确认。
- 停止循环 = 现有 TX off / STOP 端点，不新增端点。
- 设置项 `cq_loop_idle_timeout_s`：默认 600，范围 60–3600,schema 校验；非安全联锁键，不加 TX 锁。

### 错误处理

| 场景 | 行为 |
|---|---|
| 非租约持有者启动循环 | 409（同现有 CQ 拒绝语义） |
| safety 故障中布防 | 现有拒绝原样透传；循环内布防失败 → 终止循环（`arm_refused`) |
| QSO 进行中启动循环 | 沿用现有 CQ 冲突语义 |
| `idle_timeout_s` 越界 | 422 schema 拒绝 |

### 测试

- `tests/engine/test_cq_loop.py`（假时钟 / sleeper / 假 lease 源）:DONE→续呼、EXHAUSTED→续呼、PARTNER_LOST→续呼、MANUAL/STOP/故障→终止、空闲超时停、DONE 重置计时、租约丢失停、启停幂等、布防失败终止。
- API 层：循环启动的租约门控与审计、快照 `cq_loop` 字段、设置项边界（59/60/3600/3601)。
- 契约：组合层断言 `on_decode` 批次含 `dt`/`freq`/`to_me`;static 测试钉住 candidates.js 行渲染引用的字段与 service worker 缓存版本惯例。
- 文档同步：SDD §10.1(REST 契约）、§10.2（快照字段）、§11.3、第 15 章（循环安全语义：租约门控、STOP 优先、故障不重布防）、SDD/14 版本历史、tests/README。

## 验证

- `venv/bin/python -m pytest tests/` 全绿。
- `sdd_context.py check --staged` clean。
- 硬件验证（发布清单）：真台开循环，观察无应答续呼、应答后自动 QSO、QSO 后自动再 CQ、空闲超时自动停、STOP 立即停、断网 15 秒 dead-man 停。
