# 新 DXCC 高亮 + 自动呼叫 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** Band Activity 将新 DXCC 实体电台紫色高亮；FT8 设置加 "Auto-call new DXCC" 开关（后端持久化），开启后服务端无人值守自动对第一个新 DXCC 的 CQ 发起通联（不打断当前 QSO、必须过 safety.arm、无需租约）。

**架构：** `server/engine/dxcc.py` 加 `get_cty_database()` 懒加载单例（api/main 共用）；`decode_message_view` 增加 `is_new_dxcc`（cty lookup + worked 实体集合）；`on_decode` 检测第一个新 DXCC CQ → `safety.arm()` → `sequencer.reply_to`（create_task 异步）；开关存 `setting_meta`（`SETTING_SCHEMA` 加 bool validator，走现有 `/settings` PUT/GET）；前端紫色高亮 + FT8 toggle。

**技术栈：** Python 3.13 / FastAPI / vanilla JS / pytest（TDD）。**零新依赖。**

**规格：** `docs/superpowers/specs/2026-08-04-new-dxcc-autocall-design.md`

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `server/engine/dxcc.py` | `get_cty_database()` 懒加载单例（`parents[2]/cty.dat`） | 修改 |
| `server/main.py` | `decode_message_view` 加 `is_new_dxcc`；`on_decode` 自动呼叫（`maybe_auto_call` + `_auto_call`） | 修改 |
| `server/web/api.py` | `_cty_database()` 转发 `get_cty_database()`；`SETTING_SCHEMA` 加 `auto_call_new_dxcc` | 修改 |
| `server/web/static/css/app.css` | `.candidate.new-dxcc` 紫色 | 修改 |
| `server/web/static/js/candidates.js` | 行类 `new-dxcc` | 修改 |
| `server/web/static/js/api.js` | `settings()` / `putSetting()` | 修改 |
| `server/web/static/js/settings.js` | FT8 toggle（localStorage + 后端 PUT） | 修改 |
| `tests/engine/test_dxcc.py` | `get_cty_database()` 单例 | 修改 |
| `tests/web/test_main.py` | `is_new_dxcc` 判定 + 自动呼叫矩阵 | 修改 |
| `tests/web/test_api.py` | 设置 validator + 持久化 | 修改 |
| `AGENTS.md`、`SDD/05`、`SDD/11`、`SDD/12`、`SDD/14` | 文档同步 | 修改 |

**关键事实：** `on_decode` 是同步回调（orchestrator 驱动），异步动作需 `asyncio.get_running_loop().create_task()`；`safety.arm()` 是 async（TxRefused 异常表示 interlock 打开）；`sequencer.reply_to(msg, snr_db, tx_phase=1-(slot_id%2))` 后 `state=REPLYING`（≠IDLE → 天然节流）；`PUT /settings` 已有 schema 校验 + 幂等；`SETTING_SCHEMA` 在 api.py:57；`decode_message_view` 当前签名 `(message, my_call="")`，测试 `test_decode_message_view_carries_band_activity_fields` 直接调用。

---

### 任务 1：`get_cty_database()` 懒加载单例（engine 层共用）

**文件：**
- 修改：`server/engine/dxcc.py`、`server/web/api.py`
- 测试：`tests/engine/test_dxcc.py`、`tests/web/test_api.py`

- [ ] **步骤 1：编写失败的测试**

`tests/engine/test_dxcc.py` 追加：

```python
def test_get_cty_database_loads_repo_cty_singleton() -> None:
    from server.engine.dxcc import get_cty_database

    db1 = get_cty_database()
    db2 = get_cty_database()
    assert db1 is db2                      # 单例
    assert len(db1.entities) > 300         # 仓库内 cty.dat（346 实体）
    assert db1.lookup("BI1TX") == ("China", "AS")
```

`tests/web/test_api.py` 追加（转发后行为不变）：

```python
def test_dxcc_endpoint_still_works_after_singleton_refactor(
    client: TestClient, state: AppState
) -> None:
    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BI1TX", band="20m")
    )
    session_id = login(client)
    body = client.get("/api/v1/dxcc", headers=auth_headers(session_id)).json()
    assert body.get("ok") is True
    assert body["total"] >= 1
```

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/engine/test_dxcc.py::test_get_cty_database_loads_repo_cty_singleton -q`
预期：FAIL（`ImportError: cannot import name 'get_cty_database'`）

- [ ] **步骤 3：实现**（`server/engine/dxcc.py` 末尾追加）

```python
_cty_db: CtyDatabase | None = None


def get_cty_database() -> CtyDatabase:
    """Repository-root cty.dat, loaded once per process (shared by the REST
    layer and the auto-call path).  Missing/broken file → empty db."""

    global _cty_db
    if _cty_db is None:
        from pathlib import Path

        _cty_db = load_cty(str(Path(__file__).resolve().parents[2] / "cty.dat"))
    return _cty_db
```

`server/web/api.py` 的 `_cty_database()` 改为转发：

```python
def _cty_database() -> Any:
    from ..engine.dxcc import get_cty_database

    return get_cty_database()
```

（删除原 `_cty_cache` 全局 + 路径逻辑；`_cty_cache` 定义行一并删除。）

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/engine/test_dxcc.py tests/web/test_api.py -q`
预期：PASS（含既有 dxcc 缓存测试）

- [ ] **步骤 5：Commit**

```bash
git add server/engine/dxcc.py server/web/api.py tests/engine/test_dxcc.py tests/web/test_api.py
git commit -m "refactor(dxcc): shared get_cty_database() lazy singleton (api + auto-call path)"
```

---

### 任务 2：`is_new_dxcc` decode 标记

**文件：**
- 修改：`server/main.py`（`decode_message_view` + `on_decode`）
- 测试：`tests/web/test_main.py`

- [ ] **步骤 1：编写失败的测试**

`tests/web/test_main.py` 追加（FakeSlotMessage 在文件顶部已有 fixture 定义；确认 `K1ABC` 对应实体——K1 是 United States）：

```python
def test_decode_message_view_marks_new_dxcc(tmp_path: Path) -> None:
    from server.main import decode_message_view

    item = decode_message_view(FakeSlotMessage(), "M0XX")
    # FakeSlotMessage 是 K1ABC（United States 实体），空库 → 新 DXCC
    assert item["call"] == "K1ABC"
    assert item["is_new_dxcc"] is True


def test_decode_message_view_not_new_when_entity_worked(tmp_path: Path) -> None:
    from server.main import decode_message_view

    item = decode_message_view(FakeSlotMessage(), "M0XX", is_new_dxcc=False)
    assert item["is_new_dxcc"] is False
```

（实现选择：`decode_message_view(message, my_call, *, is_new_dxcc=False)` 显式传参——判定在调用方 `on_decode` 做，view 只是透传字段。这样 view 保持纯函数、测试直接构造。）

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/web/test_main.py::test_decode_message_view_marks_new_dxcc tests/web/test_main.py::test_decode_message_view_not_new_when_entity_worked -q`
预期：FAIL（`KeyError: 'is_new_dxcc'`）

- [ ] **步骤 3：实现**（`server/main.py`）

`decode_message_view` 签名加关键字参数并在返回 dict 加字段：

```python
def decode_message_view(
    message: Any, my_call: str = "", *, is_new_dxcc: bool = False
) -> dict[str, Any]:
    ...
    return {
        ...
        "mine": bool(parsed.from_call)
        and bool(my_call)
        and base_call(parsed.from_call) == base_call(my_call),
        "is_new_dxcc": is_new_dxcc,
    }
```

`on_decode` 的 batch 构造改为逐条判定（`state.dxcc_cache` 为 None 时惰性填充——复用 `dxcc_summary`）：

```python
            from .engine.dxcc import get_cty_database, dxcc_summary

            if state.dxcc_cache is None or state.repository.dxcc_dirty:
                state.dxcc_cache = await asyncio.to_thread(
                    dxcc_summary, repository, get_cty_database()
                )
                state.repository.dxcc_dirty = False
            worked_dxcc = {e.name for e in state.dxcc_cache.entities}
            views = []
            for message in slot_decode.messages:
                view = decode_message_view(message, config.my_call)
                if view["call"] and not view["mine"]:
                    entity = get_cty_database().lookup(view["call"])
                    view["is_new_dxcc"] = bool(entity) and entity[0] not in worked_dxcc
                views.append(view)
            batch = {"slot_id": slot_decode.slot_id, "late": slot_decode.late, "messages": views}
```

**注意：** `on_decode` 是同步函数，不能 `await`。用 `asyncio.get_running_loop().create_task(...)` 包一个 async 辅助做缓存填充（避免阻塞 decode 回调）；或在回调外（orchestrator 启动时）预填充一次。**推荐**：在 `create_server` 的 lifespan 里、orchestrator 启动前，`await` 一次缓存填充（与 `/dxcc` 相同的逻辑），on_decode 内仅读缓存（None 时保守 `is_new_dxcc=False`）。

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/web/test_main.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add server/main.py tests/web/test_main.py
git commit -m "feat(dxcc): decode messages carry is_new_dxcc (worked-entity check)"
```

---

### 任务 3：自动呼叫（on_decode 触发 + `_auto_call`）

**文件：**
- 修改：`server/main.py`
- 测试：`tests/web/test_main.py`

- [ ] **步骤 1：编写失败的测试**

`tests/web/test_main.py` 追加（利用 `create_server(start_dsp=False, start_audio=False)` + TestClient lifespan；`FakeSlotMessage` 若 is_cq 为 False 需造 CQ 消息——检查 FakeSlotMessage 定义后，构造 CQ 变体）：

```python
def _cq_slot_message(call: str = "W6AER", snr: int = -5) -> FakeSlotMessage:
    msg = FakeSlotMessage()
    msg.result.text = f"CQ {call} DM26"
    msg.parsed.is_cq = True
    msg.parsed.from_call = call
    msg.result.snr = snr
    return msg


def test_auto_call_fires_on_new_dxcc_cq_when_idle(tmp_path: Path) -> None:
    app = create_server(
        make_config(), rig=FakeRig(), start_dsp=False, start_audio=False
    )
    with TestClient(app):
        state = app.state.app_state
        state.repository.set_setting("auto_call_new_dxcc", True)
        state.sequencer.on_message(parse_message("M0XX CQ TEST1"), snr_db=-10)
        # 模拟 orchestrator 驱动一个 decode 槽
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            slot = _cq_slot_message("K7TEST")  # United States, 未通联
            loop.run_until_complete(_drive_decode(app, slot, 0))
        finally:
            loop.close()
        assert state.sequencer.state.value == "replying"
        assert state.sequencer.dx_call == "K7TEST"


def test_auto_call_skips_when_disabled_or_busy(tmp_path: Path) -> None:
    app = create_server(
        make_config(), rig=FakeRig(), start_dsp=False, start_audio=False
    )
    with TestClient(app):
        state = app.state.app_state
        # 开关关
        state.sequencer.on_message(parse_message("M0XX CQ TEST2"), snr_db=-10)
        slot = _cq_slot_message("K8TEST")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_drive_decode(app, slot, 0))
        finally:
            loop.close()
        assert state.sequencer.state.value == "idle"
        # 忙（已在 REPLYING）
        state.repository.set_setting("auto_call_new_dxcc", True)
        state.sequencer.reply_to(
            parse_message("M0XX CQ BUSY1"), -10, tx_phase=0
        )
        slot2 = _cq_slot_message("K9TEST")
        loop2 = asyncio.new_event_loop()
        asyncio.set_event_loop(loop2)
        try:
            loop2.run_until_complete(_drive_decode(app, slot2, 0))
        finally:
            loop2.close()
        assert state.sequencer.dx_call == "BUSY1"  # 未被抢占
```

测试辅助（文件顶部或测试内）：

```python
async def _drive_decode(app, slot_message, slot_id: int) -> None:
    """直接调用 server 内部 on_decode 的等价路径：通过 orchestrator 触发
    不可行（start_dsp=False 无 orchestrator），因此用 create_server 内部
    闭包不可达——改为在测试里直接模拟 on_decode 的逻辑：
    构造 AppState 并调用 main.decode_message_view + 手动触发 _auto_call。
    """
```

**实现约束说明：** `on_decode` 是 `create_server` 内部闭包，测试无法直接调用。**改为**：把自动呼叫判定抽成 `server/main.py` 模块级可测函数：

```python
def auto_call_candidate(
    view: dict[str, Any],
    *,
    sequencer_state: str,
    has_selection: bool,
    auto_call_enabled: bool,
) -> bool:
    """决策 A：新 DXCC CQ + 空闲 + 无人工选择 + 开关开 → 可自动呼叫。"""
    return (
        auto_call_enabled
        and not has_selection
        and sequencer_state == QSOState.IDLE.value
        and bool(view.get("is_new_dxcc"))
        and bool(view.get("is_cq"))
        and not view.get("mine")
    )
```

测试改为对 `auto_call_candidate` 做矩阵断言（纯函数，无需 orchestrator）；`on_decode` 内只调它 + `create_task(_auto_call(...))`。**这个测试架构变更由实现者决定**——若纯函数抽离更可测，就抽；测试断言行为（触发/不触发矩阵）是权威。

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/web/test_main.py::test_auto_call_fires_on_new_dxcc_cq_when_idle tests/web/test_main.py::test_auto_call_skips_when_disabled_or_busy -q`
预期：FAIL（`ImportError: cannot import name 'auto_call_candidate'` 或类似）

- [ ] **步骤 3：实现**（`server/main.py`）

```python
def auto_call_candidate(
    view: dict[str, Any],
    *,
    sequencer_state: str,
    has_selection: bool,
    auto_call_enabled: bool,
) -> bool:
    """决策 A：第一个新 DXCC CQ，空闲且无人工选择时自动通联。"""
    return (
        auto_call_enabled
        and not has_selection
        and sequencer_state == QSOState.IDLE.value
        and bool(view.get("is_new_dxcc"))
        and bool(view.get("is_cq"))
        and not view.get("mine")
    )
```

`on_decode` 内 batch 构造后追加：

```python
            # 自动呼叫（决策 A：无人值守、第一个新 DXCC CQ、不打断当前）。
            auto_enabled = (
                repository.get_setting("auto_call_new_dxcc") is True
            )
            if auto_enabled and sequencer.state is QSOState.IDLE and state.selected is None:
                for view in views:
                    if auto_call_candidate(
                        view,
                        sequencer_state=sequencer.state.value,
                        has_selection=state.selected is not None,
                        auto_call_enabled=auto_enabled,
                    ):
                        slot_id = slot_decode.slot_id
                        tx_phase = 0 if slot_id is None else 1 - (slot_id % 2)
                        asyncio.get_running_loop().create_task(
                            _auto_call(state, repository, view, slot_id, tx_phase)
                        )
                        break  # 第一个即止
```

模块级 `_auto_call`（async）：

```python
async def _auto_call(
    state: Any, repository: Any, view: dict[str, Any], slot_id: int, tx_phase: int
) -> None:
    """safety.arm（interlock）通过后走 sequencer.reply_to 完整通联。"""

    from .engine.msgparse import ParsedMessage
    from .engine.safety import TxRefused

    try:
        await state.safety.arm()
    except TxRefused:
        log.info("auto_call skipped: interlock open (%s)", view.get("call"))
        return
    except Exception:
        log.exception("auto_call arm failed")
        return
    try:
        state.selected = ParsedMessage(
            text=str(view.get("text") or view.get("call") or ""),
            is_cq=True,
            from_call=str(view.get("call") or "").upper(),
            grid=str(view.get("grid") or "").upper(),
        )
        state.selected_snr_db = view.get("snr")
        state.selected_slot_id = slot_id
        state.sequencer.reply_to(state.selected, view.get("snr"), tx_phase=tx_phase)
        await asyncio.to_thread(
            repository.record_audit,
            actor="system",
            operation="auto_call",
            target=str(view.get("call") or ""),
            detail=f"snr={view.get('snr')} new_dxcc",
        )
        log.info("auto_call: %s snr=%s slot=%d", view.get("call"), view.get("snr"), slot_id)
    except Exception:
        log.exception("auto_call failed for %s", view.get("call"))
```

`main.py` 顶部 import 补 `QSOState`（`from .engine.sequencer import DisarmReason, QsoContext, QSOState, Sequencer`）。

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/web/test_main.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add server/main.py tests/web/test_main.py
git commit -m "feat(auto-call): first new-DXCC CQ auto-QSO when idle (safety-armed, no lease)"
```

---

### 任务 4：开关设置（SETTING_SCHEMA + 持久化测试）

**文件：**
- 修改：`server/web/api.py`（SETTING_SCHEMA）
- 测试：`tests/web/test_api.py`

- [ ] **步骤 1：编写失败的测试**

`tests/web/test_api.py` 追加：

```python
def test_auto_call_setting_round_trip(client: TestClient, state: AppState) -> None:
    session_id = login(client)
    put = client.put(
        "/api/v1/settings",
        json={"auto_call_new_dxcc": True},
        headers=auth_headers(session_id),
    )
    assert put.status_code == 200
    got = client.get("/api/v1/settings", headers=auth_headers(session_id)).json()
    assert got["settings"]["auto_call_new_dxcc"] is True
    assert state.repository.get_setting("auto_call_new_dxcc") is True


def test_auto_call_setting_rejects_non_bool(client: TestClient) -> None:
    session_id = login(client)
    put = client.put(
        "/api/v1/settings",
        json={"auto_call_new_dxcc": "yes"},
        headers=auth_headers(session_id),
    )
    assert put.status_code == 422
```

- [ ] **步骤 2：运行确认失败**

运行：`venv/bin/python -m pytest tests/web/test_api.py::test_auto_call_setting_round_trip tests/web/test_api.py::test_auto_call_setting_rejects_non_bool -q`
预期：FAIL（422 invalid_setting——schema 无此键）

- [ ] **步骤 3：实现**（`server/web/api.py` SETTING_SCHEMA 加一行）

```python
    "auto_call_new_dxcc": lambda v: isinstance(v, bool),
```

（放 `cq_loop_idle_timeout_s` 之后；非 SAFETY_IMPACTING，不加 TX 锁定集合。）

- [ ] **步骤 4：运行确认通过**

运行：`venv/bin/python -m pytest tests/web/test_api.py -q`
预期：PASS

- [ ] **步骤 5：Commit**

```bash
git add server/web/api.py tests/web/test_api.py
git commit -m "feat(web): auto_call_new_dxcc persisted setting (bool, /settings)"
```

---

### 任务 5：前端 — 紫色高亮 + FT8 toggle

**文件：**
- 修改：`server/web/static/css/app.css`、`server/web/static/js/candidates.js`、`server/web/static/js/api.js`、`server/web/static/js/settings.js`

- [ ] **步骤 1：css/app.css — 紫色高亮**（`.candidate.stale` 附近加）

```css
.candidate.new-dxcc { color: #b06fdf; }
```

- [ ] **步骤 2：candidates.js — 行类**（`item.classList.add("cq")` 附近加）

```js
      if (candidate.is_new_dxcc) item.classList.add("new-dxcc");
```

- [ ] **步骤 3：api.js — settings 方法**（`dxcc:` 行之后加）

```js
  settings: () => request("/settings"),
  putSetting: (key, value) =>
    request("/settings", { method: "PUT", idempotencyKey: key(), body: { [key]: value } }),
```

- [ ] **步骤 4：settings.js — FT8 toggle + 后端同步**

`renderFt8()` 的 HTML（"Hide my own echoes" 之后）加：

```js
      <label class="setting-row toggle">
        <span>Auto-call new DXCC</span>
        <input type="checkbox" data-setting="auto_call_new_dxcc" ${s.auto_call_new_dxcc ? "checked" : ""}>
      </label>
```

现有 `[data-setting]` change handler 内，`auto_call_new_dxcc` 额外 PUT 后端（其余仍走 localStorage）：

```js
    for (const input of content.querySelectorAll("[data-setting]")) {
      input.addEventListener("change", () => {
        const value = input.type === "checkbox" ? input.checked : input.value;
        saveSettings({ [input.dataset.setting]: value });
        if (input.dataset.setting === "auto_call_new_dxcc") {
          api.putSetting("auto_call_new_dxcc", Boolean(value)).then((res) => {
            if (!res.ok) showToast(`Auto-call setting: ${res.reason || res.status}`);
          });
        }
      });
    }
```

boot 时拉后端值合并进 settings（`settings.js` 导出 `loadSettings` 保持 localStorage；在 `main.js` boot 或 `createSettingsDrawer` 内补充）：

```js
  // 后端持久化设置覆盖 localStorage 默认（auto_call_new_dxcc 权威在后端）
  api.settings().then((res) => {
    if (res.ok && res.settings) {
      const merged = { ...loadSettings(), ...res.settings };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(merged));
      patch({ settings: merged });
    }
  });
```

（放在 `createSettingsDrawer` 内、`if (!getState().settings) patch(...)` 之后；`showToast` 已在文件顶部 import。）

- [ ] **步骤 5：验证**

```bash
cd /Users/cheenle/HAM/ft8 && node --check server/web/static/js/settings.js && node --check server/web/static/js/candidates.js && node --check server/web/static/js/api.js
grep -c "new-dxcc" server/web/static/css/app.css server/web/static/js/candidates.js
```
预期：node --check 无输出；grep 计数 ≥ 2。

- [ ] **步骤 6：Commit**

```bash
git add server/web/static/css/app.css server/web/static/js/candidates.js server/web/static/js/api.js server/web/static/js/settings.js
git commit -m "feat(web): purple new-DXCC highlight + Auto-call toggle (backend-persisted)"
```

---

### 任务 6：文档同步（AGENTS.md / SDD）

**文件：**
- 修改：`AGENTS.md`、`SDD/05-non-functional-requirements.md`、`SDD/11-component-model.md`、`SDD/12-operational-model.md`、`SDD/14-version-history.md`

- [ ] **步骤 1：AGENTS.md 模块表**（`dxcc.py` 行后补一句）

```
（dxcc.py 行追加）`get_cty_database()` 懒加载单例供 API/自动呼叫共用
```

- [ ] **步骤 2：SDD/05 加 NFR**（NFR-086 行后）

```
| NFR-087 | New-DXCC auto-call | Decode messages carry `is_new_dxcc`; with setting `auto_call_new_dxcc` enabled the server auto-QSOs the first new-DXCC CQ when idle (safety-armed, no lease, never interrupts a QSO) |
```

- [ ] **步骤 3：SDD/11 组件表**（`dxcc.py` 行后）

```
（dxcc.py 行追加）`get_cty_database()` shared lazy singleton; auto-call decision in main.py `auto_call_candidate`
```

- [ ] **步骤 4：SDD/12 §12.6**（cty.dat 段后补一句）

```
Setting `auto_call_new_dxcc` (bool, persisted in setting_meta) arms unattended auto-QSO on first new-DXCC CQ when idle; safety interlock always gates TX (NFR-087).
```

- [ ] **步骤 5：SDD/14 版本历史**（顶部 Unreleased 区加条目，格式同前）

```
## Unreleased — 2026-08-04 — New-DXCC Highlight + Auto-Call

- `decode_message_view` 增加 `is_new_dxcc`（cty lookup + worked 实体集合，复用 dxcc_cache）；Band Activity 新实体行紫色高亮（`.candidate.new-dxcc`）。
- `auto_call_candidate` 纯函数 + `on_decode` 触发：开关开 + 空闲 + 无人工选择 + 第一个新 DXCC CQ → `safety.arm()` → `sequencer.reply_to`（create_task 异步，audit `auto_call`）；QSO 完成实体进 worked 自然不再触发；interlock 拒绝跳过不崩溃。
- 设置 `auto_call_new_dxcc`（bool）入 SETTING_SCHEMA，走 `/settings` PUT/GET 持久化；FT8 tab toggle（localStorage 回显 + 后端 PUT）；`get_cty_database()` 懒加载单例供 API/自动呼叫共用。
- Regressions: `test_main.py`（is_new_dxcc 判定、自动呼叫触发/禁用/忙矩阵）、`test_api.py`（设置 round-trip + 422）、`test_dxcc.py`（单例）。全量套件绿。
```

- [ ] **步骤 6：验证 + commit**

```bash
cd /Users/cheenle/HAM/ft8 && venv/bin/python -m pytest tests/ -q --ignore=tests/integration
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add AGENTS.md SDD/
git commit -m "docs: new-DXCC highlight + auto-call (SDD NFR-087)"
```
预期：全量测试 PASS（现 691 + 新增）、SDD check clean。

---

## 自检记录

- **规格覆盖度**：设计 §3.1（is_new_dxcc + 单例）→ Task 1/2；§3.2（自动呼叫）→ Task 3；§3.3（开关）→ Task 4；§3.4（前端）→ Task 5；§6 测试 → 各任务；§7 文档 → Task 6。Non-goals（只 CQ、不排队、不重试）在 `auto_call_candidate` 判定中体现。
- **占位符扫描**：Task 3 步骤 1 的测试含"实现约束说明"（on_decode 闭包不可直接测 → 抽 `auto_call_candidate` 纯函数）——这是明确的架构决策（可测性驱动），非占位符；实现者按此抽离。
- **类型一致性**：`decode_message_view(message, my_call="", *, is_new_dxcc=False)`（Task 2 一致）；`auto_call_candidate(view, *, sequencer_state, has_selection, auto_call_enabled) -> bool`（Task 3 一致）；`_auto_call(state, repository, view, slot_id, tx_phase)`（Task 3 一致）；`putSetting(key, value)` / `settings()`（Task 5 一致）；`auto_call_new_dxcc` bool validator（Task 4 一致）。
- **行为一致性**：`auto_call_candidate` 要求 `sequencer_state == "idle"`（QSOState.IDLE.value）且 `is_new_dxcc` + `is_cq` + 非 mine + 开关开 → 与设计决策 A/B 一致；触发后 `reply_to` 置 REPLYING 天然节流；完成进 worked 后 `is_new_dxcc` 变 False 永不重复。
