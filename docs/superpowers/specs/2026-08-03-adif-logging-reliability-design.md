# ADIF 日志记录可靠性 + 元数据完善 —— QSO 落库不丢失

- 日期： 2026-08-03
- 状态： 已批准（用户逐节确认 §1/§2/§3)
- SDD 引用： §7.5（持久化模型）、§11.2（Engine 层组件）、AD-014（SQLite 规范 + ADIF 导出）、NFR-072（非 void 记录导出）、UC-005（自动完成落库）、NFR-073（30 秒撤销）

## 背景与问题

生产库 `mrrc-ft8.db` 已有 9 条 completed QSO，但**每条都是** `freq_hz=0`、`band=''`、
`started_utc=''` —— 因为组合根里 1 秒 watchdog 以裸调用 `sequencer.pop_log_record()`
取记录，未传元数据。ADIF 导出因此对所有这些记录丢弃 `<FREQ>`/`<BAND>`。

用户实测观察到**整条通联记录丢失**（日志/ADIF 里完全没有）。代码审查确认三条独立的
丢失路径：

1. **写库失败即丢**：`pop_log_record()` 是破坏性弹出；`record_qso()` 抛异常时，watchdog
   的通用 `except Exception` 吞掉，记录已从 sequencer 弹出、从未入库 → 永久丢失。
2. **新 QSO 起手清掉未取记录**：`reply_to()` 与 `start_cq()` 都调用 `_reset_partner()`
   置 `_log_ready = None`。QSO 完成到 watchdog 下一次 1 秒轮询之间，用户点下一个
   Reply / CQ 或 `cq_loop.tick()` 在 DONE 上重新 CQ（`cq_loop.py:87`），都会把
   已完成未落库的记录抹掉。
3. **重启窗口**：完成与轮询之间进程退出，`_log_ready` 内存态丢失；`abort_active_qsos`
   只覆盖 SQLite 里的 `ACTIVE` 行。

根因是：**完成记录停留在 sequencer 可变状态（`_log_ready`），在“完成”与“SQLite 写”
之间有一个 1 秒异步空隙**，多个独立 actor 都能在这个窗口内毁掉或丢失它。

## 设计决策

### §1 QsoLog 持久化队列（Approach A，已批准）

新组件 `server/engine/qso_log.py` 中的 `QsoLog`，与现有循环线程纪律一致（入队与
排水都在事件循环线程，无跨线程状态）：

- **`enqueue(record)`**：由 sequencer 在完成瞬间（`_ensure_log`）调用，记录立即离开
  sequencer 状态。`_reset_partner()` / `start_cq()` / `reply_to()` 从此无法销毁它。
- **`drain_once()`**：watchdog 每秒调用，**每次至多处理一条**（维持今天 pop-once 节奏）。
  尝试 `repository.record_qso`；失败则按尝试次数回队首重试；连续失败达
  `MAX_ATTEMPTS = 5` 时**溢出到 `data/qso-pending.jsonl`**（append JSONL）并记日志——
  从内存移除以免阻塞队列，但绝不静默丢弃。
- **`recover()`**：启动时读回 dead-letter 文件并入队，然后清空文件。配合 `flush()`，
  重启不会丢任何已入册记录。
- **`flush()`**：优雅关闭时把队列与 dead-letter 剩余全部写库，随后才关闭 repository。

**Sequencer 解耦**：删除 `pop_log_record()`，注入 `on_qso: Callable[[QSORecord], None]`
回调（与 `cq_loop` 注入 `arm`/`on_audit` 同风格）。`_ensure_log` 构建完整记录并
**恰好一次**调用回调。

**组合根（main.py）**：`pop_log_record()` + `record_qso()` → `qso_log.drain_once()`；
启动在 repository 打开后 `qso_log.recover()`；关闭在 teardown 前 `await qso_log.flush()`。
`state.qso_log` 暴露，health 快照给出 `pending` 计数（“确保保存”可视化）。

### §2 元数据捕获 + ADIF 完善

- 新增 `server/engine/bands.py`：服务端 freq→band 映射（镜像前端 `band.js`：40m/20m/15m/
  10m，ADIF band 名，±50 kHz 匹配）+ `band_from_freq_hz(freq)`。
- **sequencer 捕获 QSO 起始时刻**：注入 `clock`（默认 `time.time`，仓库 DI 风格）。CQ
  被应答或 `reply_to` 时记起始 epoch；`_ensure_log` 格式化 `HHMMSS` 进 `started_utc`。
- **完成时频段上下文**：注入 `context: Callable[[], tuple[int, str]]`，`_ensure_log`
  读入 `freq_hz`/`band`。组合根接 `state.radio_freq_hz` + `band_from_freq_hz`。记录在
  入队时即完整，不再有 pop 时元数据。
- **`adif.py`**：
  - `TIME_ON` = `started_utc`（非空时），否则回退完成时刻（兼容既有 9 行）；
  - 新增 `TIME_OFF` = `completed_epoch`；
  - `FREQ`/`BAND` 一旦填充即按现有逻辑渲染，无需改 ADIF 生成。
- **历史行不回填**：既有 9 行 freq/band 事后不可知，保持无 `FREQ`/`BAND` 导出，在
  SDD 版本历史注明；新 QSO 完整。

### §3 测试与文档（已确认范围）

**TDD 测试：**
- `tests/engine/test_qso_log.py`（重写）：入队恰好一次；`drain_once` 写库；写失败重试、
  5 次后溢出到 `tmp_path` dead-letter；`recover()` 读回；`flush()` 关闭时排水；**核心回归：
  完成后立刻 `reply_to()`/`start_cq()` 不丢记录**（即实测 bug）。
- `tests/engine/test_bands.py`（新增）：`band_from_freq_hz` 含 ±50 kHz 边界。
- `tests/engine/test_sequencer.py`：`on_qso` 每次 QSO 恰好一次；起始时刻捕获。
- `tests/engine/test_adif.py`：`TIME_ON` 来自 `started_utc`；新增 `TIME_OFF`；空
  `started_utc` 回退。
- `tests/web/test_api.py`：health 含 `qso_log.pending`；`/logs/adif` 仍导出。
- `tests/engine/test_repository.py`：不改。

**SDD 同步**（AGENTS.md 铁律 + sdd-guardian）：§7.5 持久化模型（QsoLog 队列 +
dead-letter）、§11.2 模块表（`qso_log.py` 职责、新增 `bands.py`）、§14 版本历史。
运行时文件 `data/qso-pending.jsonl` 加 `.gitignore`。

**明确不做（YAGNI）**：`cty.dat` 的 DXCC/CQZ/ITUZ/CONT 国家字段；磁盘上自动写
`.adi` 文件。ADIF 仍是按需导出、SQLite 为规范源。

## 验收标准

- 完成一条 QSO 后立即触发 `reply_to()`/`start_cq()`/重启，记录仍落库（回归测试覆盖）。
- 写库失败时重试并最终进 dead-letter，进程重启后 `recover()` 找回。
- 新 QSO 的 ADIF 含 `FREQ`/`BAND`/`TIME_ON`（起始）/`TIME_OFF`（完成）。
- 既有 9 行不受影响，仍可导出（无 `FREQ`/`BAND`）。
- 全部 pytest 通过；SDD §14 版本历史新增条目。
