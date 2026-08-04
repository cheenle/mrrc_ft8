# MRRC-FT8 新 DXCC 高亮 + 自动呼叫 — Design

**Date:** 2026-08-04
**Status:** Draft for review
**Scope:** Band Activity 里把"新 DXCC 实体"的电台行紫色高亮；FT8 设置加"Auto-call new DXCC"开关（后端持久化），开启后服务端无人值守地自动对第一个新 DXCC 的 CQ 发起通联。

## 1. Purpose

DXCC 统计已上线（186 实体缓存）。现在要把"新 DXCC"变成可见的、可自动操作的信号：新实体电台在 Band Activity 紫色高亮；可选开启自动呼叫，让服务器 24h 守着、新 DXCC 一出现就自动通联（不依赖浏览器在线）。

Confirmed decisions (brainstorm, 2026-08-04):
- 触发：**A — 服务端无人值守**。`on_decode` 里检测新 DXCC CQ → 自动发起完整 QSO；不打断当前 QSO；同实体完成即进 worked，自然不再触发。
- 选台：**B — 解码顺序第一个**新 DXCC CQ 消息。
- 开关：**A — 后端持久化**。`setting_meta` 加 `auto_call_new_dxcc`（bool）；FT8 tab toggle → `PUT /settings`；重启不丢；多浏览器一致。
- 自动呼叫**只对 CQ 消息**；**不抢**人工选择/进行中的 QSO；**无需控制租约**（系统级）但**必须过 `safety.arm()`**（interlock 检查）。

## 2. Current Structure

- `server/main.py:on_decode` — 同步回调（orchestrator 驱动），构造 `decode_message_view` batch 推 WS；`decode_message_view(message, my_call)` 返回 `{text, snr, dt, freq, call, grid, is_cq, to_me, mine}`。
- `server/engine/sequencer.py:reply_to(msg, snr_db, tx_phase)` — 设置 `state=REPLYING`、`tx_enabled=True`，之后 TX 链路自动跑完 QSO。
- `server/web/api.py:operation_reply` — 人工路径：`safety.arm()` → `sequencer.reply_to(state.selected, ...)`。`SETTING_SCHEMA`（57 行）、`/settings` GET/PUT、`_read_settings` 已有。
- `server/engine/dxcc.py` — `CtyDatabase.lookup(call)`、`dxcc_summary(repository, cty)`；`AppState.dxcc_cache` + `Repository.dxcc_dirty` 缓存（NFR-086）。
- `server/web/static/js/settings.js` — `renderFt8()` 渲染设置项（`[data-setting]` change → localStorage `saveSettings`）；`candidates.js:render()` 按 `settings`/`workedCalls` 过滤渲染 `.candidate` 行；`api.js:request()` 统一 REST 封装。

## 3. Design

### 3.1 `is_new_dxcc` 判定（decode 消息标记）

- `server/main.py:decode_message_view` 增加参数 `is_new_dxcc: bool = False`（保持向后兼容，测试直接构造）。
- 调用处（`on_decode` 的 batch 构造）改为先算：

```python
worked_dxcc = {e.name for e in state.dxcc_cache.entities} if state.dxcc_cache else set()
views = []
for message in slot_decode.messages:
    view = decode_message_view(message, config.my_call)
    if view["call"] and not view["mine"]:
        entity = _cty_database().lookup(view["call"])
        view["is_new_dxcc"] = bool(entity) and entity[0] not in worked_dxcc
    views.append(view)
```

- `_cty_database()` 复用 api.py 的懒加载单例——为共用，移至 `server/engine/dxcc.py` 新增 `get_cty_database() -> CtyDatabase`（模块级全局懒加载，路径 `Path(__file__).resolve().parents[2] / "cty.dat"`），api.py 的 `_cty_database()` 改为转发 `get_cty_database()`，main.py 直接用它。避免双实例。
- **缓存一致性**：`dxcc_cache` 在 dirty 时由 `/dxcc` 接口重建；`on_decode` 读到的是已缓存集合（首次读取前为 None → 先经 `/dxcc` 或自动呼叫路径触发重建；实现时 `worked_dxcc` 为 None 时惰性调用 `dxcc_summary` 填充 `state.dxcc_cache`）。

### 3.2 自动呼叫（main.py `on_decode`）

在 batch 构造后追加：

```python
def maybe_auto_call(view: dict, slot_id: int) -> None:
    """决策 A：空闲 + 无人工选择 + 开关开 + 新 DXCC CQ → 自动通联。"""
    if not view.get("is_new_dxcc") or not view.get("is_cq") or view.get("mine"):
        return
    if sequencer.state is not QSOState.IDLE or state.selected is not None:
        return  # 不打断当前 QSO / 不抢人工选择
    if not auto_call_enabled():
        return
    asyncio.get_running_loop().create_task(
        _auto_call(view, slot_id)
    )
```

- `auto_call_enabled()`：`repository.get_setting("auto_call_new_dxcc") is True`（读后端设置，thread-safe；每次 decode 读一次 SQLite——低频，可接受；或缓存）。
- `_auto_call(view, slot_id)`（async，create_task 包装）：

```python
async def _auto_call(view: dict, slot_id: int) -> None:
    try:
        await state.safety.arm()   # interlock 检查；拒绝则本槽跳过
    except TxRefused:
        log.info("auto_call skipped: interlock open")
        return
    state.selected = ParsedMessage(
        text=view["text"], is_cq=True, from_call=view["call"].upper(),
        grid=(view.get("grid") or "").upper(),
    )
    state.selected_snr_db = view["snr"]
    state.selected_slot_id = slot_id
    tx_phase = 0 if slot_id is None else 1 - (slot_id % 2)
    state.sequencer.reply_to(state.selected, view["snr"], tx_phase=tx_phase)
    await asyncio.to_thread(
        repository.record_audit, actor="system",
        operation="auto_call", target=view["call"],
        detail=f"snr={view['snr']} new_dxcc",
    )
```

- **节流**：触发后 `sequencer.state = REPLYING` ≠ IDLE → 后续槽不再触发；QSO 完成 → 实体进 worked → `is_new_dxcc` 变 False → 永不重复。safety 拒绝（interlock fault）→ 本槽跳过，下槽重试（arm 持续拒绝则每次跳过，可接受）。
- **租约**：不 acquire lease（系统级；人工会话的 lease 不受影响）。`safety.arm()` 是 TX 唯一许可路径（与人工 reply 相同）。
- **并发**：`_auto_call` 与人工 reply 可能竞争——`sequencer.state == IDLE` 前置检查 + `safety.arm` 互斥（arm 后只有本次 TX 上下文）；极端竞争下后者 arm 失败即跳过，无双重 TX。

### 3.3 开关（后端持久化 + 快照回显）

- `SETTING_SCHEMA` 加：
  ```python
  "auto_call_new_dxcc": lambda v: isinstance(v, bool),
  ```
  （非 SAFETY_IMPACTING，不 TX 锁定。）
- `PUT /settings` 已支持任意 schema 键（幂等 + 校验）→ 前端 `PUT {"auto_call_new_dxcc": true}` 即可。
- `GET /settings` 已返回 `{settings: {...}, schema}` → boot 时前端拉取开关状态。
- 前端 `api.js` 加：
  ```js
  settings: () => request("/settings"),
  putSetting: (key, value) => request("/settings", { method: "PUT", idempotencyKey: key(), body: { [key]: value } }),
  ```

### 3.4 前端高亮 + 开关

- `candidates.js:render()` 行类加：
  ```js
  if (candidate.is_new_dxcc) item.classList.add("new-dxcc");
  ```
- CSS（`server/web/static/css/app.css`，`.candidate` 定义在 84 行附近）加：
  ```css
  .candidate.new-dxcc { color: #b06fdf; }  /* 紫色高亮新 DXCC */
  ```
- `settings.js:renderFt8()` 加 toggle（镜像现有 `[data-setting]` 项，但走后端）：
  ```js
  <label class="setting-row toggle">
    <span>Auto-call new DXCC</span>
    <input type="checkbox" data-setting="auto_call_new_dxcc" ${s.auto_call_new_dxcc ? "checked" : ""}>
  </label>
  ```
  change 事件：`saveSettings`（localStorage 回显）**加** `await api.putSetting("auto_call_new_dxcc", checked)`；boot 时 `GET /settings` 合并进 settings（后端值优先于 localStorage 默认）。
  - 注意：现有 `renderFt8` 的 `[data-setting]` 统一 change handler 走 localStorage；`auto_call_new_dxcc` 需要单独 handler（localStorage + 后端 PUT）。

### 3.5 Non-goals

- 不做"呼叫我的消息自动回复"（只 CQ）；不做排队逐个通联（第一个即止，完成后再等下一个新实体）；不做定时重试（safety 拒绝即跳过）。
- 不改 `is_new_dxcc` 判定精度（基于 186 实体缓存；新实体 QSO 完成 → dirty → 下次重建自动排除）。
- 不做前端持久化的行为差异（localStorage 仅用于 UI 立即回显，权威状态在后端）。

## 4. Data Flow

decode 槽 → `on_decode` → 每条消息 `decode_message_view` + `is_new_dxcc`（cty lookup + worked 实体集合）→ batch 推 WS（前端紫色高亮）→ 若 `auto_call_new_dxcc` 开且空闲：第一个新 DXCC CQ → `safety.arm()` → `sequencer.reply_to`（TX 链路自动跑完）→ 完成 → 实体进 worked → 不再高亮/不再触发。

## 5. Error Handling

- cty 未加载（None）：`is_new_dxcc` 全 False（不误触发自动呼叫），`log.warning` 一次。
- `safety.arm()` TxRefused：跳过本槽，无副作用；后续槽重试。
- `get_setting` 异常：`auto_call_enabled()` 返回 False（安全默认关闭）。
- `_auto_call` 内任何异常：`log.exception`，不影响 on_decode 主流程（create_task 内 try/except 兜底）。

## 6. Testing

- `tests/engine/test_dxcc.py`：`get_cty_database()` 懒加载单例。
- `tests/web/test_main.py`（或 test_api.py）：
  - `decode_message_view` `is_new_dxcc`：未通联实体 True / 已通联 False / 无 call / mine False。
  - 自动呼叫矩阵（用 `create_server(start_dsp=False, start_audio=False)` + FakeRig + 造 decode 消息）：
    - 开关开 + 空闲 + 新 DXCC CQ → 触发（sequencer.state == REPLYING、audit `auto_call`）。
    - 开关关 → 不触发。
    - 忙（REPLYING）→ 不触发。
    - 已通联实体 → 不触发。
    - 非 CQ 消息 → 不触发。
    - safety fault（interlock）→ 不触发且不崩溃。
  - 设置：`PUT /settings {"auto_call_new_dxcc": true}` → `GET /settings` 回读 true；非法类型 422。
- 前端：`node --check` + 手动（toggle → 高亮 → 自动通联）。
- 文档：AGENTS.md、SDD/05（NFR-087）、SDD/11、SDD/12、SDD/14。

## 7. Deployment / Docs

- 无新 Python 依赖。`cty.dat` 复用现有懒加载（改共用 `get_cty_database()`）。
- `.env` 无需新变量（开关是运行时设置，非启动配置）。
