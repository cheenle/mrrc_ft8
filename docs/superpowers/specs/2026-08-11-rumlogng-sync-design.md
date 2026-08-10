# RUMLogNG 双向同步程序 — 设计规格

日期：2026-08-11 · 状态：已批准（用户逐节确认）
SDD 关联：AD-014（SQLite canonical records）、NFR-072（canonical persistence）、§12.7（Backup and Retention）、§11.2（ADIF 生成）

## 1. 背景与目标

本机同时运行：

- **MRRC-FT8 headless 服务器**：QSO 记录在 `data/mrrc-ft8.db`（qso 表，当前 10299 条 completed，source=jtdx，从 JTDX `wsjtx_log.adi` 每小时导入）。
- **RUMLogNG**（macOS 沙盒日志软件，正在运行）：Core Data SQLite `~/Library/Containers/de.dl2rum.RUMlogNG/Data/Library/Application Support/RUMLogNG/CoreQsoModel_1.sqlite`（ZCORE_QSO 表，14746 条：FT8 13791 + SSB 944 + FT4 11），监听 UDP `*.2237`（WSJT-X 协议接收口）。

两边已有大量重叠历史（同一批 FT8 QSO 经不同路径进入两边），且存在差异（RUMLogNG 有 2026-08-10 的新 QSO 与 SSB 记录，FT8 db 最新为 2025-09-07 的 jtdx 导入）。

**目标**：单独创建一个本地双向同步程序，任一边新增 QSO 时定期（每 5 分钟）同步到另一边，最终两边日志记录集合一致。

## 2. 需求决策（用户确认）

| # | 决策点 | 选择 |
| --- | --- | --- |
| D1 | 同步范围 | **B. 全量**：RUMLogNG 中 SSB/FT4 也进 FT8 db（qso.mode 已有字段），两边完全一致 |
| D2 | 初始历史 | **A. 一次性全量合并**：启动首轮双向合并现有记录（按去重键），此后只同步新增 |
| D3 | 周期 | **C. 每 5 分钟一轮**（crontab `*/5` 驱动，程序跑一轮退出） |
| D4 | 字段冲突 | **A. 以 RUMLogNG 为准**：同一 QSO 字段冲突时 RUMLogNG 的值覆盖 FT8 db |
| D5 | 部署形态 | **B. 手动运行 + crontab**：`python -m rumlog_sync` 每轮跑完退出；不用 launchd 常驻 |
| D6 | RUMLogNG 接入 | **方案 1**：FT8→RUMLogNG 走 UDP 2237（WSJT-X `QSO_LOGGED`）；RUMLogNG→FT8 只读轮询 Core Data SQLite。**绝不写 RUMLogNG 数据库** |

## 3. 架构

### 3.1 程序形态

- 独立顶层包 `rumlog_sync/`，**纯 Python 标准库**（sqlite3 / socket / json / datetime / fcntl），不 import `server/` 任何模块（解耦，"单独创建"）。
- 入口 `python -m rumlog_sync`：执行一轮同步后退出；crontab `*/5` 驱动；`flock` 文件锁防重入（上一轮未完成则本轮直接退出）。
- 配置 `data/rumlog-sync.json`：

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

- 模块划分：
  - `__main__.py` — CLI 入口、flock、加载配置、调一轮 sync
  - `rumlog_reader.py` — 只读打开 Core Data SQLite、列存在性探测、按 Z_PK 游标取新增记录
  - `mapper.py` — Core Data 行 ↔ QSORecord 字段映射（见 §4）
  - `wsjt_udp.py` — 构造并发送 WSJT-X HEARTBEAT / QSO_LOGGED UDP 包（可注入 socket 便于测试）
  - `ft8_db.py` — FT8 db 只写访问：去重查询、INSERT/UPDATE、rumlog_uuid/pushed 状态更新
  - `sync.py` — 一轮同步编排（拉 → 合并 → 推 → 状态持久化）
  - `state.py` — `rumlog_sync_state` 表读写 + qso 表幂等迁移（ADD COLUMN 探测）

### 3.2 一轮同步流程

```
Step 1 拉（RUMLogNG → FT8 db）
  1. uri mode=ro 打开 CoreQsoModel_1.sqlite（WAL 并发读安全；RUMLogNG 运行中亦可读）
  2. 列存在性探测（ZCORE_QSO 关键列：ZCALLSIGN/ZDATETIME/ZQRG/ZMODE/ZBAND/ZLOCATOR/ZRSTTX/ZRSTRX/ZUUID）——缺列即本轮报错退出，防 RUMLogNG 升级
  3. SELECT Z_PK > last_seen_pk 的记录
  4. 逐条映射 → 跨库去重匹配（§5）→
     不存在 → INSERT（source='rumlog'，rumlog_uuid=hex UUID）
     已存在 → 以 RUMLogNG 为准 UPDATE（report_sent/report_rcvd/dx_grid/band/mode/freq_hz）+ 若 rumlog_uuid 空则回填
  5. 更新 last_seen_pk = max(Z_PK)，写 last_sync_at

Step 2 推（FT8 db → RUMLogNG）
  1. SELECT pushed_to_rumlog=0 的记录
  2. 逐条构造 WSJT-X QSO_LOGGED ADIF 包（§6），UDP 发 127.0.0.1:2237
  3. 标记 pushed_to_rumlog=1（乐观）
  4. 闭环确认：后续轮次在 RUMLogNG 读到匹配记录 → 回填 rumlog_uuid（确认送达）
  5. 已推送但连续 confirm_retries 轮未在 RUMLogNG 出现（UDP 丢包）→ 重置 pushed_to_rumlog=0 重推；超上限记日志告警
```

## 4. 字段映射（Core Data → FT8 qso）

| ZCORE_QSO 列 | FT8 qso 字段 | 说明 |
| --- | --- | --- |
| ZCALLSIGN | dx_call | 对方呼号 |
| ZDATETIME | completed_epoch | Core Data 时间戳（自 2001-01-01 秒），+978307200 → Unix epoch |
| ZDATETIME | started_utc | 由 epoch 推导 HHMMSS（近似起始时刻） |
| ZQRG (MHz float) | freq_hz | round(MHz × 1e6) |
| ZMODE | mode | FT8/FT4/SSB… |
| ZBAND | band | 原样 |
| ZLOCATOR | dx_grid | 可空 |
| ZRSTTX / ZRSTRX | report_sent / report_rcvd | 去 `+` 号转 int，非法值置 NULL |
| ZUUID (BLOB) | rumlog_uuid | hex 编码 |
| —（RUMLogNG 无我方字段） | my_call / my_grid | 取配置（默认 BG1SB / ON80DA） |

对**已存在**的记录（跨库匹配命中），`completed_epoch`/`started_utc` **不覆盖**（FT8 侧为 JTDX 精确值）；仅新插入时使用由 ZDATETIME 推导的值。
| — | status | 'completed' |
| — | source | 'rumlog'（拉入的） |
| — | void_actor / void_reason | NULL |

FT8 db → RUMLogNG 推送的 ADIF 记录：`CALL`=dx_call、`GRIDSQUARE`=dx_grid、`MODE`=mode、`BAND`=band、`FREQ`=freq_hz/1e6、`RST_SENT`/`RST_RCVD`、`QSO_DATE`/`TIME_ON`/`TIME_OFF`（由 completed_epoch 派生）、`STATION_CALLSIGN`=my_call。

## 5. 跨库去重规则（关键）

同一 QSO 在两边完成时刻相同（最多差几秒），因此：

- **第一优先（UUID 幂等）**：FT8 db 记录 `rumlog_uuid` 非空 → 直接与 RUMLogNG ZUUID 精确匹配。
- **主匹配**：`dx_call` + `band` 相同 **且** `|completed_epoch − z_epoch| ≤ 120s` → 视为同一 QSO。
- 插入前同样检查（含与"本批已插入"的临时集），杜绝重复插入。
- 推送确认回填也用同一匹配规则。

## 6. WSJT-X UDP 协议细节

- 每轮推送前先发一条 `HEARTBEAT`：
  `<MessageType:9>HEARTBEAT<Id:NN>MRRC-FT8-SYNC<DialFrequency:11>14074000<...>`（符合 WSJT-X 注册习惯，对 RUMLogNG 无害；按 WSJT-X UDP spec 构造完整字段）。
- `QSO_LOGGED` 消息：
  `<MessageType:10>QSO_LOGGED<ADIF:NNN>…ADIF 记录…<EOR>` + 0x00 结尾。
- 发送目标 `127.0.0.1:2237`（已确认 RUMLogNG 监听 `udp4 *.2237`）。
- 开发冒烟：向 RUMLogNG 发一条测试 QSO 验证落库（`--smoke` 旗标，明确提示事后手动删除）。

## 7. 错误处理与恢复

- RUMLogNG db 打开失败 / 缺列 / 解析坏行 → 记日志、跳过该记录或整轮、**不崩溃、不半途写状态**。
- 只读连接（`mode=ro`），RUMLogNG 运行中读写皆安全。
- UDP 发送失败 → 记日志，记录保持 pushed_to_rumlog=0，下轮重推。
- 每轮末尾写 `last_sync_at`；同步事件写入 FT8 db `audit_event`（actor='rumlog_sync'，operation='rumlog_pull'/'rumlog_push'，detail=计数）。

## 8. FT8 数据库变更（幂等迁移）

- `qso` 表加列：`rumlog_uuid TEXT NOT NULL DEFAULT ''`
- `qso` 表加列：`pushed_to_rumlog INTEGER NOT NULL DEFAULT 0`
- 新表 `rumlog_sync_state(key TEXT PRIMARY KEY, value TEXT)`：last_seen_pk / last_sync_at / schema_version
- 迁移通过 `PRAGMA table_info` 探测列存在，`ALTER TABLE ADD COLUMN` 幂等执行；不触碰服务器运行时表结构语义（服务器 SELECT 均按列名，新增列向后兼容）。

## 9. 测试计划（pytest）

- fixture：按 ZCORE_QSO 真实 schema 构造模拟 RUMLogNG db（含 WAL 下并发读场景）+ 临时 FT8 db。
- 用例：初始全量合并去重；增量拉取（Z_PK 游标）；字段冲突以 RUMLogNG 为准（UPDATE 回写）；120s 窗口去重；UUID 幂等（重跑不重复）；推送标记生命周期（0→1→确认回填→丢包重推）；UDP 包格式（mock socket，断言 HEARTBEAT+QSO_LOGGED 序列与 0x00 结尾）；状态游标持久化；flock 防重入；损坏行跳过；列探测失败明确报错。
- 真实 RUMLogNG 集成：`@pytest.mark.skipif`（默认跳过，需本机 RUMLogNG 运行）。
- 测试不向真实 2237 端口发包（mock）。

## 10. SDD / 文档同步（sdd-guardian Phase 5）

- SDD/08 新增 AD-016（RUMLogNG 双向同步：UDP 推送 + 只读 Core Data 轮询；禁写 RUMLogNG 库）。
- SDD/12 §12.7 扩展（同步状态与迁移说明）。
- SDD/14-version-history.md 新条目（V1.8 → V1.9）。
- AGENTS.md 模块表加 `rumlog_sync/` 行。
- tests/README.md 覆盖清单更新。
- constraints.json / index.json 登记新约束（如"RUMLogNG 数据库只读访问；禁直接写 Core Data"）。

## 11. 部署

crontab（手动安装，用户偏好 B）：

```
*/5 * * * * cd /Users/cheenle/HAM/ft8 && flock -n data/rumlog-sync.lock venv/bin/python -m rumlog_sync >> data/rumlog-sync.log 2>&1
```

首次运行自动完成全量合并；日志与状态在 `data/`。若 FT8 服务器与同步程序同时写 mrrc-ft8.db：SQLite 默认 journal 并发安全，服务器经自身 repository 层写、同步程序独立连接写，均短事务；如发现锁竞争可在后续调优（WAL 模式检查）。

## 12. 范围外（YAGNI）

- 不做 RUMLogNG 的编辑/删除/冲突三方合并（RUMLogNG 侧删记录不回删 FT8 db）。
- 不做 launchd 常驻（用户选 crontab）。
- 不做多用户/多电台。
- 不写 RUMLogNG 数据库（硬约束）。
