# FT-710 滤波器宽度无法通过软件切换 —— 调查与修复记录

日期：2026-08-03
状态：**已修复**（根因已更正；初版根因分析有误，见 §4）

## 1. 现象

- 在 MRRC-FT8 Web 驾驶舱的 Radio 抽屉里切换滤波器宽度（1.8 / 2.4 / 3.0 kHz），
  API 返回 200（rigctld 确认收到命令），但 FT-710 实际宽度不变或短暂变化后"回退"。
- 用户实测：14.074 MHz 当前 3 kHz，切到 2.4 kHz "切不过来"。
- 日志证据：`POST /radio/mode` 返回 200，rigctld 无错误，但电台宽度未保持。

## 2. 环境

| 项 | 值 |
|---|---|
| 电台 | Yaesu FT-710 |
| Hamlib | 4.6.2 (2025-02-09, SHA=8703647c，MacPorts) |
| rigctld 启动 | `rigctld -m 1049 -r /dev/cu.usbserial-0121DB3A0 -s 38400 -T 127.0.0.1 -t 4532 -vvv` |
| 架构约束 | AD-008：rigctld 是唯一串口 owner；应用不直接碰串口 |

## 3. 初版调查（结论后来被推翻）

| 路径 | 当时观察 | 当时结论 |
|---|---|---|
| rigctld `M <mode> <pb>` | `m` 读回瞬时 2400 → 1-3 秒后 1800 | "电台回退默认宽度" |
| rigctld `\send_raw 0 SH00NN;` | 回复 `No answer`，宽度"未变" | "后端不透传字节" |
| 停 rigctld 直连串口 `SH00NN;` | 宽度持久 | "唯一可靠路径，但违反 AD-008" |

由此一度实现了"直接串口 quick-poke"方案（提交 ca9ec89，含错误索引
2400→13）与 `\send_raw` 透传方案的尝试。**两者基于的根因都是错的。**

## 4. 更正后的根因（hamlib 4.6.2 源码级确认，SHA=8703647c）

用 rigctld TCP 的原始命令原语（`w` / `W` / `\send_raw`）直接读写电台
SH 寄存器，交叉验证 `m` 的读数后，确认 hamlib 4.6.2 的 FT-710 后端
（Yaesu newcat）**两个方向都有缺陷**：

### 4.1 SET 缺陷：`M <mode> <width>` 根本没改宽度

`newcat_set_rx_bandwidth`（rigs/yaesu/newcat.c）给 FT-710 拼的 CAT 命令
格式错了。命令格式选择分支：

```c
if (is_ftdx101d || is_ftdx101mp || is_ft891)  → "SH%c%d%02d;"
else if (is_ft2000 || is_ftdx3000)            → "SH0%02d;"
else if (is_ftdx10)                           → "SH00%02d;"   // ← FT-710 不在这里！
else                                          → "SH%c%02d;"   // ← FT-710 落到这里
```

FT-710 要求 4 位参数 `SH00NN;`，而 4.6.2 发出的是 3 位的 `SH0NN;`
（如 `SH014;`）——**畸形帧，电台直接忽略**。所以 `M USB 2400` 返回
RPRT 0（MD 成功），宽度却纹丝不动。

实测：基线 3000 → `M USB 2400` → RPRT 0 → 0.3 s / 3.3 s 后原始读回仍
`SH0020;`。

（master 分支已修复：`is_ftdx10 || is_ft710 || is_ftx1` → `"SH00%02d;"`。）

### 4.2 GET 缺陷：`m` 读回的宽度是错的

`newcat_get_rx_bandwidth` 在 4.6.2 里**没有 FT-710 分支**（master 才加入
`... || is_ft710`），FT-710 落进 FT-450/FT-9000 的兜底分支：

```c
if (w < 16)      *width = rig_passband_narrow(rig, mode);  // FT-710 SSB = 1800
else if (w > 16) *width = rig_passband_wide(rig, mode);    // FT-710 SSB = 3000
else             *width = rig_passband_normal(rig, mode);  // FT-710 SSB = 2400
```

| 真实宽度 | SH 索引 | `m` 报告 | 是否正确 |
|---|---|---|---|
| 1800 | 9  | 1800（narrow） | ✓ 巧合 |
| 2400 | 14 | **1800（narrow）** | ✗ 错误 |
| 3000 | 20 | 3000（wide） | ✓ 巧合 |

### 4.3 "1-3 秒回退"与"重启重置"都是 GET 缺陷的表象

- `rig_set_mode` 之后 hamlib 缓存（`widthMainA`，默认 TTL 500 ms）暂时
  返回设定值 2400；缓存过期后真实读取走 §4.2 的错误映射 → 1800。
  **电台从未回退**——任意时刻用原始 `SH0;` 读寄存器都是设定值。
- `newcat_open` 不触碰 SH 寄存器，**rigctld 重启不会重置宽度**；
  "重启后 `m` 读回 1800" 同样是 §4.2 的误读。

### 4.4 `\send_raw` 一直是好的

`\send_raw` 的参数语义是 `\send_raw <期望回复描述> <命令>`：第一个参数
`0` = 不期望回复、`;` = 读到 `;` 为止，**不是 VFO**。`No answer` 是
无回复命令的**正常成功输出**。实测 `\send_raw 0 SH0014;` 可靠改写宽度、
`\send_raw ; SH0;` 可靠读回（换行结尾）。初版"不透传"结论源于把
`No answer` 当成失败、并用同样有缺陷的 `m` 验证结果。

## 5. 修复方案（已实现）

原则：**保持 AD-008**——不直接开串口，所有 CAT 字节仍经 rigctld；只用
rigctld 的原始命令透传补上 hamlib 的缺陷。

1. **写宽度**：`RigClient.set_filter_width(hz)` →
   `\send_raw 0 SH00<NN>;`（索引表 `{1800: 9, 2400: 14, 3000: 20}`，
   即 hamlib `ftdx101_ssb_widths` 的下标）。rigctld 回复 `No answer`；
   出现 `RPRT` 才视为失败。
2. **读宽度**：`RigClient.get_filter_width()` → `\send_raw ; SH0;`，
   解析 `SH00NN;` 并按 `ftdx101_ssb_widths` 表换算 Hz。这是电台的真实
   宽度，不受 §4.2 误读影响。
3. **API**：
   - `GET /radio/mode` 的 `passband_hz` 优先取 §2 的原始读数，失败才退回
     hamlib `m` 的值（抽屉始终显示电台真实宽度）。
   - `POST /radio/filter` 走 §1。
   - `POST /radio/mode` 在 `M` 之后尽力补一次 §1（hamlib 4.6.2 下 `M`
     不会应用宽度）；失败只告警，不影响模式设置的成功响应。
4. **清理**：删除直接串口方案（ca9ec89 的 pyserial 依赖、
   `FT710_FILTER_SERIAL_PORT` 环境变量、requirements.txt）；修正错误索引
   （2400→14，此前误用 13=2300 Hz）；SDD AD-008 无需豁免（未曾真正豁免）。

### 实测验证（rigctld 在位、服务器轮询运行中）

```
w SH0009; / w SH0014; / w SH0020;      → SH0; 读回分别为 0009/0014/0020，持久
\send_raw 0 SH0014;（rig 在 1800）     → No answer；SH0; 读回 0014 ✓
M USB 2400（rig 在 3000）              → RPRT 0；SH0; 读回仍 0020（SET 缺陷复现）
m（rig 在 2400）                       → 1800（GET 缺陷复现）
```

单元测试：`tests/engine/test_rig.py`（FakeRigctld 只接受 4 位 `SH00NN;`
帧，模拟真实电台对畸形帧的忽略）、`tests/web/test_api.py`（含
"hamlib 谎报 1800、真实寄存器 2400" 的场景）。

## 6. 后续

- 上游：hamlib master 已修复两处（SET 格式分支加 is_ft710；GET 加 FT-710
  分支）。若日后升级 hamlib，本修复依然兼容：原始 SH 命令是稳定 CAT 语义，
  且 `m` 届时读数变准，与原始读数一致。
- 可考虑向 hamlib 报告 4.6.2 的这两个 FT-710 缺陷（若尚未有 issue）。

## 7. 参考

- Hamlib 4.6.2 (8703647c): `rigs/yaesu/newcat.c`
  （`newcat_set_mode` / `newcat_set_rx_bandwidth` / `newcat_get_rx_bandwidth`）、
  `rigs/yaesu/ftdx101.c`（`ftdx101_ssb_widths`）、`tests/rigctl_parse.c`
  （`w` / `W` / `\send_raw` 语义）、`src/rig.c`（`rig_send_raw`、缓存 TTL 500 ms）
- FT-710 CAT 手册：`SH — WIDTH` 命令（P1=0, P2=0, P3=00-23）
