# 设备配置可配置化设计（HAMLIB 电台 + 音频设备）

日期：2026-08-10 · 状态：已批准（v2）
需求来源：用户 —— "FT8 调整为可以修改电台设备（通过 HAMLIB 的 daemon）、音频设备（可选择）的可配置，并在菜单栏里设置，设备设置后可以应用（重启应用）"

## 1. 目标与范围

- 在 UI（桌面客户端 + 手机 PWA）中配置：
  - **电台设备**：完整 HAMLIB 参数 —— rig 型号（hamlib model）、串口设备、波特率、rigctld 监听端口（选项 A）
  - **音频设备**：单一设备下拉，同时用于 RX 采集与 TX 播放（延续 `MRRC_FT8_AUDIO_DEVICE` 语义）
- 「保存」只落盘不重启；「应用重启」写配置后自动重启 rigctld + server
- 串口唯一 owner 仍是 rigctld（AD-008 不破坏）；server 仍只经 Hamlib TCP 说话

## 2. 配置存储 — `data/device-config.json`

字段（全部可选，缺省回退 env）：

| 键 | 类型 | 说明 | env 对应 |
|---|---|---|---|
| `rig_model` | int | hamlib 型号 | `MRRC_FT8_RIG_MODEL`（restart.sh） |
| `rig_device` | str | CAT 串口路径 | `MRRC_FT8_RIG_DEVICE`（restart.sh） |
| `rig_baud` | int | 波特率 | `MRRC_FT8_RIG_BAUD`（restart.sh） |
| `rigctld_port` | int | rigctld 监听/连接端口 | `MRRC_FT8_RIGCTLD_PORT` + `MRRC_FT8_RIGCTLD`（server 连接） |
| `audio_device` | str\|int\|null | 音频设备名或索引；null/缺省 = System default | `MRRC_FT8_AUDIO_DEVICE`（server） |

加载规则：
- server 启动：`ServerConfig.from_env()` 之后，若文件存在则覆盖对应字段（rigctld host 恒为 `127.0.0.1`，仅 port 可变）
- restart.sh：若文件存在 → python3 读 JSON 覆盖 RIG_* 变量；否则用 env（现状，完全向后兼容）
- 不含任何 secret（密码 hash 仍在 `.env`）

写规则：
- server（PUT）以**原子写**（临时文件 + `os.replace`）落盘
- `data/` 已在 .gitignore，配置不入库

## 3. API（`server/web/api.py`）

权限：与现有 settings 一致（`require_session`）；安全锁与 `SAFETY_IMPACTING_SETTINGS` 相同模式（TX active → 409）。

### 3.1 `GET /api/v1/devices`
返回：
```json
{
  "config": {
    "rig_model": 1049, "rig_device": "/dev/cu.usbserial-0121DB3A0",
    "rig_baud": 38400, "rigctld_port": 4532,
    "audio_device": "USB Audio Device"
  },
  "source": {"rig_model": "env", "audio_device": "env", ...},  // env | file | default
  "audio_devices": [{"index": 0, "name": "MacBook Pro Speakers", "max_input": 0, "max_output": 2}],
  "serial_devices": ["/dev/cu.usbserial-0121DB3A0", ...],
  "rig_status": {"connected": true, "host": "127.0.0.1", "port": 4532}
}
```
- `audio_devices`：`sounddevice.query_devices()` 过滤后映射（index/name/max_input/max_output）
- `serial_devices`：`glob.glob("/dev/cu.*")`（macOS）；其他平台空列表
- `rig_status`：读当前 RigClient 的 host/port + `connected`

### 3.2 `PUT /api/v1/devices`
- body：`{"rig_model": int, "rig_device": str, "rig_baud": int, "rigctld_port": int, "audio_device": str|int|null}`
- 校验：
  - `rig_model`：正整数（在精选型号集合或任意正整数）
  - `rig_device`：非空字符串，`/dev/` 开头（允许任意路径以便非 macOS 平台）
  - `rig_baud`：∈ {1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200}
  - `rigctld_port`：int ∈ [1024, 65535] 且 ≠ 8000
  - `audio_device`：null，或存在于 `audio_devices` 枚举（按 name 或 index 匹配）
- TX active → 409 `{reason: "tx_active"}`
- 成功 → 原子写 JSON → `{ok: true, saved: true}`（不重启）

### 3.3 `POST /api/v1/devices/apply`
- 前置：TX active → 409；`data/device-config.json` 不存在 → 409 `{reason: "no_device_config"}`
- 动作：`setsid` 分离 spawn `restart.sh`（stdout/stderr → `/tmp/mrrc-ft8-restart.log`），随后**不**由 server 主动退出（restart.sh 会 kill 它自己）
- 返回 202 `{restarting: true, eta_s: 20}`（8s CoreAudio 释放 + rigctld/server 启动 + 就绪等待的估算）
- 幂等键支持（沿用现有 IdempotencyCache 机制）

## 4. `restart.sh` 修改（最小 diff）

顶部解析段：`data/device-config.json` 存在时用 python3 读 JSON 覆盖：
```bash
if [ -f "data/device-config.json" ]; then
  eval "$(python3 - <<'PY'
import json
with open("data/device-config.json") as f: c = json.load(f)
m = {"MRRC_FT8_RIG_MODEL": c.get("rig_model"), "MRRC_FT8_RIG_DEVICE": c.get("rig_device"),
     "MRRC_FT8_RIG_BAUD": c.get("rig_baud"), "MRRC_FT8_RIGCTLD_PORT": c.get("rigctld_port")}
for k, v in m.items():
    if v is not None: print(f"{k}={v!r}")
PY
)"
fi
```
- 注意：先于现有 `RIG_MODEL="${MRRC_FT8_RIG_MODEL:-1049}"` 读取即可天然回退 env
- 其余流程（杀进程 / 8s / 串口守卫 / 拉起 / 就绪等待）**原样不动**
- apply 由 server `setsid` 分离 spawn，杀 server 不影响脚本

## 5. 桌面客户端（`desktop/ft8web/src/App.tsx` + `mrrcClient.ts`）

### 5.1 `mrrcClient.ts`
新增：
```ts
mrrc.devices(): ApiResult                    // GET /devices
mrrc.saveDevices(cfg): ApiResult             // PUT /devices (idempotency key)
mrrc.applyDevices(): ApiResult               // POST /devices/apply (idempotency key)
```

### 5.2 Settings 弹窗新增「Devices」分区（弹窗最顶部，站级硬件）
- **Radio**：
  - rig 型号下拉：`FT-817 (1020)` / `FT-710 (1049)` / `IC-7300 (3073)` / `IC-M710 (30003)` + 「自定义型号…」数字输入（逃生口）
  - 串口下拉：`serial_devices` 列表 + 手动输入选项
  - 波特率下拉：1200–115200 常见值
  - rigctld 端口输入（数字）
- **Audio**：设备下拉（`audio_devices` 枚举 name；含 "System default (None)" 项，值为 null）
- 显示当前生效值 + 来源标识（env / file / default）
- 按钮：
  - **「保存」** → `saveDevices()`；TX 时 409 → `settingsError`（"Devices locked during TX"），弹窗保持打开
  - **「应用重启」** → 确认框（"将重启 rigctld 与服务器，断开约 20 秒"）→ `applyDevices()` → 显示 "Restarting…" 状态 → WS 断连重连后自动刷新
- 打开弹窗时并行拉 `mrrc.devices()`；保存成功后可重新拉取以刷新来源标识

## 6. 手机 PWA（`server/web/static/`，vanilla JS，无构建步骤）

### 6.1 `api.js`
新增 `devices() / saveDevices(cfg) / applyDevices()`（沿用现有 envelope/idempotency-key 模式）。

### 6.2 ☰ 抽屉新增「Devices」标签页（`index.html` drawer-tabs + `settings.js` renderTab）
- `renderDevices()`：同款表单（型号下拉、串口下拉+手动、波特率、端口、音频下拉）
- 「保存」→ toast 成功/失败；TX 锁 → toast 提示
- 「应用并重启」→ `confirm()` 确认 → `applyDevices()` → toast "Restarting…" → streams 重连后自动重绘
- 表单选项数据来自 `api.devices()`；初始为占位态，加载完成后填充

## 7. 客户端重连

- 桌面：已有 mrrcStreams WS 重连；断开期间显示 "Restarting…"；重连成功后 `openSettingsModal`/设备区自动刷新（状态快照拉取已存在）
- PWA：streams.js 已有重连；toast 提示重启中；重连后自动重绘抽屉

## 8. 测试

### 8.1 API 单测（`tests/`）
- mock `sounddevice.query_devices` 与 `glob`：
  - GET：config/source/枚举/rig_status 结构
  - PUT：合法值落盘格式正确（原子写）、非法值 400/422、TX active → 409、audio_device 不在枚举 → 400
  - apply：TX active → 409、无配置文件 → 409、成功 → 202 且 spawn 调用一次（mock subprocess/setsid）
  - env 回退：无 JSON 文件时 `ServerConfig` 与 GET 均显示 env 来源
- 原子写与 JSON 解析 helper 单独单测

### 8.2 restart.sh 解析逻辑
- 将 JSON→env 覆盖逻辑提取为可测的 python 片段或 bash 函数；单测：JSON 存在覆盖、缺字段回退 env、无 JSON 原行为

### 8.3 UI
- 桌面 vitest：Devices 区渲染（型号/波特率/音频选项）、保存 TX 锁提示、确认框→apply 流程（mock mrrcClient）
- PWA：`settings.js` 新增 renderDevices 的 DOM 测试（现有测试基础设施内）

### 8.4 回归
- 全量 `venv/bin/python -m pytest tests/` + `desktop/ft8web` vitest

## 9. SDD 同步

- SDD §12.6 配置章节：新增 `data/device-config.json` 与优先级规则
- AD-008：注记 —— rigctld 仍为串口唯一 owner；其参数改由配置文件驱动（UI 可改），server 仍只走 Hamlib TCP
- AGENTS.md 模块表：server/web/api.py 行注记 devices 端点；restart.sh 行注记 JSON 读取
- SDD/14-version-history.md 新条目 + SDD/README Quick Facts 版本 bump
- tests/README.md 覆盖清单同步

## 10. 明确不做（YAGNI）

- 手机与桌面分开的输入/输出音频设备（保持单一设备）
- rigctld 由 server 进程直接拉起（仍由 restart.sh 拉起，AD-008/部署职责不变）
- 远程（公网）改设备配置后的自动 SSH 重启链路（本机部署场景：restart.sh 即可）
- 型号下拉之外的自定义 rig 参数（除非选「自定义型号…」数字输入）
- 应用重启时的健康检查/回滚（保持现状：失败时 CAT 红，restart.sh 重试逻辑照旧）
