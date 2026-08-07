# 新 DXCC 自动呼叫 —— 端到端机制说明

> 本文档完整阐述 MRRC-FT8 的"新 DXCC 自动呼叫"（auto-call）功能：从空中信号解码，
> 到实体判定、自动应答、QSO 完成、落库刷新的全链路机制、配置、安全设计与运维观察点。
> 适用版本：SDD V1.3（2026-08-07，含 UC-003/UC-004 频率纪律修复）。

---

## 1. 功能定位

`auto_call_new_dxcc` 是服务端的**无人值守 DXCC 追猎**开关：当解码带内出现一个
**尚未通联过的 DXCC 实体的 CQ**，且系统空闲（无 QSO、无人为选择、无发射中），
服务端自动按标准 FT8 流程应答并完成一次 QSO，全程无需操作员干预。

目标（NFR-087）：DXCC 实体通联自动化。实体已通联后不再触发（worked 集实时刷新），
因此重复通联同一实体不会发生。

### 1.1 名词

| 术语 | 含义 |
|---|---|
| DXCC 实体 | 按 `cty.dat`（country-files ADIF 格式）划分的通联计分单位，如 Niger（TN 前缀） |
| worked 集 | `qso` 表中所有非 void QSO 的呼号（去 `/后缀`）对应的已通联实体名集合 |
| is_new_dxcc | 解码消息的发起呼号 → 实体名 ∈ 实体表 且 ∉ worked 集 |
| interlock | safety 层互锁（CAT/audio/DSP/clock），任一 faulted 时拒绝 arm/TX |
| tx_phase | 发射奇偶（0=偶时隙，1=奇时隙），应答必须发在伙伴消息的相反时隙 |
| tx_frequency | TX 音频偏移（Hz）；应答跟随伙伴解码频率，CQ 选空闲频点 |

---

## 2. 端到端链路总览

```
             ┌────────────── 12 kHz int16 单声道（铁律）──────────────┐
   FT-710 ──►│ capture_proc 子进程 → UtcRing(绝对序号) → DSP Worker  │
             └────────────────────────┬───────────────────────────────┘
                                      │ 每 15 s slot 一批解码
                                      ▼
                        orchestrator.on_decode（main.py）
        ┌───────────────────────────┬───────────────────────────────┐
        │ 1. DXCC cache 刷新        │ 2. decode_message_view        │
        │    （dxcc_dirty 时同步重建，│     is_new_dxcc = lookup(call)│
        │     ~0.2 s，至多每 slot 一次）│      ∈ 实体 ∧ ∉ worked      │
        └───────────────────────────┴───────────────┬───────────────┘
                                                    ▼
        3. auto_call_candidate 决策（纯函数，每 slot 至多命中第一个）
             开关开 ∧ 空闲 ∧ 无选择 ∧ is_new_dxcc ∧ is_cq ∧ 非自身回波
                                                    │
                                                    ▼
        4. _auto_call：safety.arm()（互锁门）→ sequencer.reply_to()
              tx_phase = 1 − slot%2；tx_frequency = 伙伴解码频率（UC-003）
              失败/互锁打开 → 记日志跳过，下一 slot 重试；audit 落 auto_call
                                                    │
                                                    ▼
        5. TX 链路：TxDriver（时隙奇偶泵 + I9 决策窗口 + fit guard）
              sequencer.next_tx_message() → dsp_encode（48 kHz 波形）
              → safety.transmit（PTT 门 + watchdog + 聚合预算）
                                                    │
                                                    ▼
        6. sequencer QSO 状态机（NFR-055：1 发 + 至多 3 重传）
              REPLYING → REPORT → ROGER_REPORT → ROGERS → SIGNOFF/DONE
                                                    │
                                                    ▼
        7. 落库：_ensure_log → on_qso → qso_log.enqueue
              → record_qso（qso 表 source='live'）→ dxcc_dirty = True
                                                    │
                                                    ▼
        8. 下一个 slot：DXCC cache 重建 → 该实体进 worked → 不再触发
```

---

## 3. 环节详解

### 3.1 信号采集与解码

- 音频采集运行在**独立子进程** `capture_proc`（进程边界隔离 CoreAudio 会话退化，
  macOS 实测：残留客户端会让新会话永久劣化）。数据进 `UtcRing`（按绝对序号
  `slot_id % capacity` 寻址，杜绝时间戳错位）。
- 解码器输入恒为 **12 kHz int16 单声道**（`server/core/binding.py` 强制，其他采样率
  拒绝进入 DSP ABI）；TX 波形恒为 **48 kHz**。所有 DSP 调用经全局锁、只在独立
  Worker 内执行（OpenMP 线程只在 Fortran 聚合结果，绝不回调 Python）。
- 每 15 s slot，`orchestrator` 把整槽音频送 Worker 解码（WSJT-X 3.0.2 Improved，
  profiles 0–4，A8 门控），产出 `DecodeResult`（slot_id / snr / dt / frequency /
  text 等），经 `on_decode` 进入业务层。

### 3.2 DXCC 实体判定

- 实体库：仓库内 `cty.dat`（country-files ADIF 格式），由 `server/engine/dxcc.py`
  的 `CtyDatabase` 解析：
  - `=CALL` 精确前缀优先；
  - `(数字)` 替换（如 `(23)`）在加载时展开；
  - 其余按**最长前缀**匹配（如 `TN` → Niger）。
  - `lookup` 已索引化（精确表 + 前缀 trie），单次亚微秒。
- 判定（`main.py` on_decode）：

  ```python
  entity = get_cty_database().lookup(view["call"])      # (实体名, 洲)
  view["is_new_dxcc"] = bool(entity) and entity[0] not in worked_dxcc
  ```

  `worked_dxcc` 来自 `dxcc_summary` 全量统计（qso 表非 void 记录 × cty 查表，
  同实体同波段计一次，DXCC Challenge 语义）；cache 未建时保守为 `False`（不误触发）。

### 3.3 决策门（`auto_call_candidate` 纯函数）

每 slot 按解码消息顺序检查，**至多命中第一个**候选：

```python
return (
    auto_call_enabled            # 后端开关 setting_meta.auto_call_new_dxcc == True
    and not has_selection        # 无人工选择（P7 修复后 QSO 结束会清 selected）
    and sequencer_state == QSOState.IDLE.value   # 无 QSO、无 CQ 循环
    and bool(view.get("is_new_dxcc"))
    and bool(view.get("is_cq"))
    and not view.get("mine")     # 自身发射回波（from_call == 本台）不触发
)
```

### 3.4 触发与安全门（`_auto_call`）

- **不需要控制租约**（系统级、无人值守，NFR-087）；但发射必须过 `safety.arm()`——
  这是唯一的 TX 门。
- `arm()` 在任一互锁 faulted 时抛 `TxRefused` → 记 `auto_call skipped: interlock
  open (<call>)`，**跳过本 slot，下一 slot 重试**（不崩溃、不风暴）。
- arm 成功 → 记录选中消息（call/grid/snr/slot）→ `sequencer.reply_to(...)`：
  - `tx_phase = 0 if slot_id is None else 1 - (slot_id % 2)`（UC-003 反相时隙）；
  - `tx_frequency = 伙伴解码频率`（UC-003 频率纪律，2026-08-07 修复；旧行为写死
    1500 Hz，见 §7 复盘）；
- audit 落库：`audit_event`（actor=`system`, operation=`auto_call`, target=呼号,
  detail=`snr=… new_dxcc`）。

### 3.5 应答发射（TX 链路）

- `TxDriver`（`server/engine/tx_driver.py`）是时隙奇偶泵：只在 sequencer 的
  `tx_phase` 对应的奇偶 slot 拉取至多一条消息，编码后交 `safety.transmit()`。
- 决策窗口（I9）：sequencer 空闲时驱动持续轮询到 `TX_DECISION_CUTOFF_SECONDS`
  (5.0 s)，点击/自动呼叫一 arm 就发；**fit guard** 拒绝超过 ~2.4 s 的启动
  （12.64 s 波形必须塞进 15 s slot），错过则顺延到下一可用 slot。
- 频率：`encoder.encode(message, sequencer.tx_frequency, slot_id)`——应答用伙伴
  解码频率（split 行为，对方接收机能配对）；CQ 用 `FrequencyOccupancy` 选出的
  1500 附近空闲频点（UC-004，见 §3.8 注）。
- 安全：`transmit()` 前复核权限链（faults / armed / PTT 状态 / 聚合 TX 预算）；
  PTT 经 rigctld（串口唯一 owner）；watchdog 与 STOP 可随时取消。

### 3.6 QSO 状态机（sequencer）

应答侧流程（我们是应答者）：

```
REPLYING      Tx1: <dx> <my> <my-grid>          （先发我们的呼号+网格）
REPORT        Tx2: <dx> <my> -snr               （对方报告到达后）
ROGER_REPORT  Tx3: <dx> <my> R-snr
ROGERS        Tx4: <dx> <my> RR73               （对方 R 报告到达后）
SIGNOFF/DONE  Tx5: <dx> <my> 73 → 落库          （对方 RR73/73 到达后）
```

- 每条消息 1 发 + 至多 3 重传（NFR-055）；伙伴有相关进展消息则重传预算重置。
- 伙伴转呼他人 → anti-QRM 自动停（`PARTNER_LOST`）；预算耗尽 → `RETRY_EXHAUSTED`
  停并保留伙伴上下文；两者都触发 `on_stop`（清除 selected，P7 修复）。
- `on_message` 只处理 addressed-to-me 消息；自身回波（`mine`）被忽略。

### 3.7 落库与 worked 集刷新

- QSO 完成（收到 RR73/73）→ `_ensure_log` **恰好一次** → `on_qso` →
  `qso_log.enqueue`（`asyncio.to_thread` 落 `repository.record_qso`；
  进程崩溃时 `data/qso-pending.jsonl` 待办恢复，不丢记录）。
- `record_qso`：qso 表新增行（`source='live'`、`status='completed'`、
  my/dx call+grid、report 双方、started_utc、freq_hz、band）+ qso_event +
  `dxcc_dirty = True`。
- 下一 slot 的 on_decode 先查 `dxcc_dirty`：为真则重建 `dxcc_summary`（~0.2 s，
  至多每 slot 一次）→ 该实体进入 worked → 不会再触发（P1 修复：cache 刷新独立于
  band-hunt 开关，auto-call 不再重复呼叫刚通联的台）。

### 3.8 关联功能：自动波段狩猎（band_hunt）

- `auto_band_hunt` 开启后，band-hunter 轮询外部 `/api/band_hunt`（pskreporter 侧，
  HTTP 是唯一跨库边界），`rank_bands`/`decide_switch` 纯函数按"未通联实体数"
  排序，空闲时经 rig 调谐切频 → 把波段切到新 DXCC 最密集的地方 → 自动呼叫闭环
  自然接续。双闸门：设置开关 + 冷却（默认 1200 s）。
- **CQ 空闲频点**（UC-004，2026-08-07）：`FrequencyOccupancy` 记录最近 120 s 解码
  频率（含自身回波），`pick_cq_frequency` 在 1500±300 Hz 螺旋扫描第一个与所有
  占用中心距 ≥30 Hz 的整数频点；全占用回退 1500。手动 CQ 与 cq_loop 每次
  CQ/re-CQ 重选。

---

## 4. 配置与开关

| 项 | 途径 | 默认 | 说明 |
|---|---|---|---|
| `auto_call_new_dxcc` | Web UI FT8 tab / `PUT /api/v1/settings` | **关闭** | 后端持久化（setting_meta 表）；boot 时后端值覆盖前端默认 |
| `auto_band_hunt` | 同上 | 关闭 | 波段狩猎第二闸门 |
| `MRRC_FT8_BAND_HUNT_URL` | 环境变量 | 空 = 关闭 | pskreporter `/api/band_hunt` 地址 |
| `MRRC_FT8_BAND_HUNT_COOLDOWN` | 环境变量 | 1200 s | 切波段冷却 |
| `MRRC_FT8_LOG_LEVEL` | 环境变量 | DEBUG（restart.sh） | 观察 auto_call 日志 |

> 注意：开关在 `setting_meta` 表；**服务端开关从未开启时功能完全静默**（历史
> 教训：2026-08-05 现场 root cause 之一）。开启后可在 `/state` 快照与
> `audit_event` 表确认。

---

## 5. 运维观察点

### 5.1 日志（`/tmp/mrrc-ft8.err.log`，`MRRC_FT8_LOG_LEVEL=DEBUG`）

| 日志行 | 含义 |
|---|---|
| `slot N: M msgs, ...` + `slot N msg: snr=… dt=… f=… <text>` | 每槽解码明细 |
| `auto_call: <call> snr=… slot=…` | 自动呼叫已触发（arm 成功、已入 sequencer） |
| `auto_call skipped: interlock open (<call>)` | 互锁 faulted/PTT 不确定，跳过本槽（下槽重试） |
| `safety fault: audio: degraded capture session…` + `safety cleared: audio` | 音频退化互锁 锁/解 |
| `safety arm: TX armed by operator` | arm 成功（含 auto_call 内部调用，措辞沿用人工路径） |
| `tx window slot N: … deferring` | fit guard 顺延 |
| `band_hunt tick: auto=… seq=… selected=… armed=…` | band-hunt 状态 |

### 5.2 数据库（`mrrc-ft8.db`，SQLite）

| 表 | 观察 |
|---|---|
| `qso` | `source='live'` 行 = 本服务完成；`status`/`completed_epoch` |
| `audit_event` | `operation='auto_call'`（actor=system）每次触发一行 |
| `decode_event` | 7 天保留；消息/SNR 历史 |
| `setting_meta` | 开关持久化 |

```sql
-- 最近自动呼叫记录
SELECT * FROM audit_event WHERE operation = 'auto_call' ORDER BY epoch DESC LIMIT 10;
-- 最近 live QSO
SELECT * FROM qso WHERE source = 'live' ORDER BY id DESC LIMIT 10;
```

### 5.3 频点占用健康

`ring slot N: … gaps=1 …` 或 `caprestarts` 增长 = 采集链路退化信号；
`rms` 与 `msgs` 长时间矛盾（热而零解码）→ AUDIO fault（有 B1 复验窗口自动解除，
TX 仍需人工 re-arm，符合 no-recovery-auto-resumes-TX）。

---

## 6. 安全设计（不变量）

1. **单点 TX 门**：所有 TX 意图必经 `SafetyController`（AD-007）；Web/DSP/sequencer
   都不能直接 key PTT。auto-call 的 arm 与人工 arm 走同一互锁。
2. **互锁矩阵**：CAT / audio / DSP / clock 任一 faulted → arm 拒绝、PTT-off；
   fault 按源 latch，重复上报不刷屏。
3. **不自动恢复 TX**：fault 清除后仍需人工 re-arm（或新的 auto-call 触发），
   恢复流程绝不自行 resume TX（§12/§15）。
4. **STOP 优先**：任何已认证会话可 STOP（无需租约），幂等、PTT-off 非阻塞。
5. **不打断 QSO**：auto_call_candidate 要求 IDLE + 无选择；进行中的 QSO/人工选择
   绝不被自动呼叫打断（AD-012 单 QSO 自动化）。
6. **频率纪律**（2026-08-07）：应答发在伙伴解码频率（split），避免离频呼叫不被
   对方自动序列配对；CQ 避开占用频点。
7. **不碰 CAT 串口**：唯一 owner 是 rigctld（AD-008）。

---

## 7. 现场案例复盘（TN8GD，2026-08-07）

**背景**：TN8GD（尼日尔，新 DXCC）在 20 m 活跃，多流 pileup。

| 时间（本地） | 事件 |
|---|---|
| 06:07–07:16 | TN8GD 的 CQ 被 auto-call 候选命中 61 次，全部 `skipped: interlock open` —— 05:27 起音频退化 fault 锁互锁 3.5 h（08:55:47 才清除），TX 被安全系统正确拦截；引擎每 30 s 坚持重试，机制正常 |
| 09:46:15 | 互锁解除后第一个可用 slot，`auto_call: TN8GD snr=-21 slot=119071144` 立即触发（无需人工），发出 4 次 12.64 s 应答波形 |
| 09:46:30–09:48:00 | 解码到 3 次 `TN8GD BG1SB ON80`（f=1500）——解析为 from=BG1SB，即**自身发射回波**，非 TN8GD 应答；全程无 `BG1SB TN8GD …` 消息 |
| ~09:48:22 | 无伙伴报告，重传预算耗尽 → RETRY_EXHAUSTED → QSO 丢弃未落库 |
| 09:53–09:55 | TN8GD 转去通联 JA4AQS / VU2KPH |

**根因**：TX 音频频率写死 1500 Hz，而 TN8GD 的 CQ 在 **f=843 Hz**——应答发在错误
音频偏移，对方自动化 DX 台未配对。对照：成功 QSO（BD6KDG@1600、UT5ZC@1401）
均因对方人工在自己频率上应答；UA4LDP 同样只有 1500 回波、同样未完成。

**修复（UC-003）**：`Sequencer.tx_frequency` 记录伙伴解码频率（`reply_to(…,
tx_frequency=…)`），`TxDriver` 编码读 `sequencer.tx_frequency`；auto-call /
select / reply 全链路透传 `freq`；旧客户端缺 freq 回退 1500。同批完成 UC-004
（CQ 选空闲频点）。

---

## 8. 已知限制与后续

- **弱信号**：auto-call 不设 SNR 门槛，-21 dB 也触发（NFR 目标是"抓新实体"）；
  弱信号 QSO 成功率低，可考虑加可选最小 SNR 设置。
- **多流 DX 台**：pileup 模式下对方自动化序列可能不配对离频/拥挤呼叫；频率纪律
  已修复主要路径，但极端 pileup 仍需人工介入。
- **guard/窗口参数**：`pick_cq_frequency` 的 guard=30 Hz、窗口 ±300 Hz、
  占用 TTL 120 s 均为模块常量，可按现场拥挤度调整。
- **单一 QSO**：AD-012 约束一次只跟一个台；QSO 完成后自动转回 idle（或 cq_loop
  re-CQ），不做连续多目标追猎。
- **数据源**：`cty.dat` 是仓库内静态副本；实体划分更新需手动替换文件 + 重启
  （cache 懒加载单例）。
