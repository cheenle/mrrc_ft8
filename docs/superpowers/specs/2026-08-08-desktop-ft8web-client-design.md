# 桌面版 FT8 界面 —— ft8web 壳 + 服务器大脑（/desktop/）

- 日期： 2026-08-08
- 状态： 已批准（用户确认设计）
- 范围： 基于 MRRC-FT8 server/client 架构，用改装后的 FT8web 作为桌面客户端；不动现有移动 PWA（`server/web/static/`）
- 参考项目： `./FT8web`（ok1cdj/FT8web，GPL v3，纯参考，保持原样）

## 1. 目标

MRRC-FT8 当前只有横屏移动 Web 驾驶舱（`server/web/static/`）。用户 clone 了 FT8web（浏览器内 FT8/FT4 客户端）作为界面参考，要求：

1. 深度分析 FT8web（已完成：UI 布局 + 内部数据流）。
2. 基于本仓库的 server/client 架构，**单独创建**桌面版 FT8 界面。
3. **不动现在的界面**（`server/web/static/` 移动 PWA 保持原样）。

经澄清锁定架构：**ft8web 壳 + 服务器大脑** —— 保留 FT8web 的 React+Tailwind 界面与布局，但把它的浏览器内 DSP/音频/CAT/FSM 全部替换为调用 MRRC-FT8 服务器的 REST + 三路 WebSocket。服务器本体只做两处**增量**改动，不破坏现状。

## 2. 架构总览

```
浏览器 /desktop/                     服务器 (FastAPI)
┌──────────────────────────┐   REST   ┌───────────────────────────────┐
│ desktop/ft8web (React)   │◄───────►│ /api/v1: session/lease/       │
│  · mrrcClient.ts  (新增) │          │   operation/radio/logs/dxcc/  │
│  · mrrcStreams.ts (新增) │  WS ×3   │   settings/diagnostics        │
│  · 移除本地 DSP/CAT/FSM  │◄───────►│ /ws/v1: state/decodes/        │
└──────────────────────────┘          │   waterfall                   │
                                      └───────────────────────────────┘
```

- 客户端源码：`desktop/ft8web/`（FT8web 克隆的改装副本，去掉嵌套 `.git`，纳入本仓库）。
- 纯净参考：`./FT8web/` 保持原样（含其 `.git`），加入 `.gitignore` 防误提交。
- 服务器挂载 `/desktop` → `desktop/ft8web/dist`（Vite 构建产物，`base:'/desktop/'`）。
- `/` 根路径仍重定向到移动 PWA（`/static/index.html`），行为不变。

## 3. 换脑映射表

| FT8web 本地"脑" | 换成服务器接口 | 说明 |
|---|---|---|
| 音频采集+解码（`AudioWorklet`+`ft8-worker.ts`） | `/ws/v1/decodes` | 服务器 DSP 出解码批（`{slot_id, late, messages[]}`），客户端只渲染 |
| waterfall（`AnalyserNode.getByteFrequencyData`） | `/ws/v1/waterfall`（WF01 二进制帧） | 解析 WF01 → `Uint8Array` 频谱行喂给现有 `drawWaterfall()`（0–3000 Hz 同构） |
| 收发 FSM（`FT8FSM.ts`） | 服务器 sequencer + `/operation/*` | CQ → `operation/cq`；Ans → `operation/reply`（或 `select`）；状态显示 → 快照 `sequencer.state` |
| TX 编码+放音（`encodeFT8`+`AudioBufferSourceNode`） | 服务器 `audio_tx` | 客户端**不发声**，不发 PTT |
| CAT（`CatManager`/WebSerial/WebUSB） | `/radio/band\|mode\|filter\|rig/level` + 快照 `radio.freq_hz` | 电台全在服务器侧（rigctld 唯一串口 owner） |
| 日志（`LogBook.ts` IndexedDB） | `/logs/qsos` + `/logs/adif` | 服务器 QSO 库为唯一真源；只读 + void |
| DXCC（`DxccService` cty.dat） | `/dxcc` + 解码消息自带 `is_new_dxcc` | 免去本地 cty.dat；N/W 徽章用服务器判定 |
| 设置（localStorage） | `/settings`（decoder_profile/threads/waterfall_lps/cq_loop_timeout/auto_call_new_dxcc/auto_band_hunt）+ 本地显示偏好 | 服务器设置为权威；显示偏好仍存 localStorage |
| 登录 | `/session/login` + cookie | **新增登录页**（ft8web 原本无鉴权） |

## 4. 界面改造要点

**保留 ft8web 壳：** 整体布局、Band Activity（时间/SNR/频率/报文 + B4/N/W/DXCC 徽章）、Active QSO 面板、waterfall 画布、波段条、TX 操作区（CQ/Ans/TX Enable/状态）、设置弹窗、日志查看器、深/浅色主题、UTC 时钟 + 时隙进度条、VersionInfo/WhatsNew。

**改造点：**
- **新增登录页**：密码 → `/session/login`（cookie）；服务重启使会话失效时回到登录。
- **"Activate Audio" → 服务器连接指示**：三路 WS 的连接状态灯（state/decodes/waterfall）。
- **CQ / Ans / TX Enable** → 服务器意图；CQ/Ans 前若无控制租约则**隐式取租约**（与移动 PWA 一致，UC-002）；租约持有期间心跳；显示 OBSERVER/CONTROLLER 状态；STOP TX 无需租约（NFR-038）。
- **Auto Sequence 状态显示** → 服务器 `sequencer.state`（IDLE/CALLING/REPLYING/REPORT/…）映射到 ft8web 的 FSM 状态标签。
- **波段条 → 服务器 4 波段**（40/20/15/10m，与 `server/engine/bands.py` 一致）；**隐藏 FT4**（服务器跑 FT8 15s 时隙）。
- **Active QSO 面板** → decode 流里 `to_me`/`mine` 过滤 + 快照 `last_tx`。
- **N/W 徽章语义**（与 ft8web 不同，需明示）：`is_new_dxcc` 是服务器按**全局已通联实体集**判定的（非按当前波段）；B4 徽章用快照 `station.worked_calls`（当前波段已通联呼号）。
- **VU 表** → 服务器 AUDIO 互锁灯（浏览器端无音频）。
- **移除**：本地 DSP、CAT 设置、Wavelog/Cloudlog、PSKReporter、外部数据流、Wake Lock、音频设备选择、FT4 切换、waterfall 点击改 TX 频率（服务器决定 TX 频率；改为点击解码选中目标）。
- **VFO 频率显示** → 快照 `radio.freq_hz`（只读展示，服务器轮询 rig）。

## 5. 服务器侧改动（仅 2 处增量，不破坏现状）

1. `server/main.py`：挂载 `/desktop` → `desktop/ft8web/dist`（与 `/static` 平级新增，同 `_NoCacheStaticFiles` no-cache 策略）。
2. `server/main.py` + `server/web/api.py`：state 快照新增 `last_tx: {text, freq_hz, slot_id} | null` —— `tx_driver` 每次真实发射时把消息记入 `AppState.last_tx`，快照带上。现有 PWA 的 `applySnapshot` 只取已知键，自动忽略，零影响。

其余零改动：现有 REST/WS 契约、移动 PWA、安全规则（Host/Origin、租约、STOP 免租约）全部不变。

## 6. 开发 / 交付 / 验证

- 构建：`cd desktop/ft8web && npm install && npm run build`（需 Node + npm；`@e04/ft8ts` 是 github 依赖）。
- 开发：`npm run dev`，`vite.config.ts` 加 proxy `/api` `/ws` → `http://127.0.0.1:8000`（loopback 服务器），cookie 经 proxy 同源生效。
- 测试：
  - 新增 `mrrcClient` / `mrrcStreams` 的 vitest 单测（mock `fetch`/`WebSocket`）：解码批→行、WF01→频谱行、租约隐式获取、断线重连。
  - 现有 `pytest tests/` 保持全绿（服务器增量改动不影响现有行为；`last_tx` 加一条快照测试）。
  - 无电台时：手动起 loopback 服务器 + `npm run dev`，验证登录、解码流、waterfall 渲染、无 TX。
  - 真机（FT-710 站点）：验证 CQ/Reply 闭环、日志落库、DXCC 徽章。
- `.gitignore`：追加 `FT8web/`（纯净参考）；`desktop/ft8web/node_modules/`、`desktop/ft8web/dist/`。

## 7. 许可证

ft8web 是 **GPL v3**。改装件 `desktop/ft8web/` 保持 GPL v3，保留 `LICENSE` 与署名。MRRC-FT8 服务器本体（`server/`、`dsp/`）不含 ft8web 代码，不受牵连。

## 8. 明确不做（v1 范围外）

- 浏览器内音频/解码/编码（全部走服务器）。
- 客户端 CAT / PTT / 串口 / WebUSB。
- Wavelog/Cloudlog/PSKReporter/外部数据流接入。
- FT4 模式切换。
- waterfall 点击设 TX 频率；手动编辑报文；多 QSO 队列排序。
- 改移动 PWA 或根路径重定向行为。
