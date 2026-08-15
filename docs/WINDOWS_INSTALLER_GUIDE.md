# MRRC_FT8 Windows 桌面版安装指南

本文介绍 MRRC-FT8 整台站的 Windows 安装包（`MRRC_FT8-Setup.exe`）。包内含
服务器 + FT8 DSP 解码/编码库 + Hamlib `rigctld` + 桌面 Web 客户端，装到
**Windows 11 / Windows 10（x64）** 电脑上直接运行，控制本机连接的电台，
并支持局域网/远程访问。用户**无需**手动安装 Python 或 Node.js。

> 打包方（构建/发布）操作手册见仓库根目录 [`win_pack.md`](../win_pack.md)。

## Download (v1.2.0 Stable)

| File | Size | SHA-256 |
|------|------|---------|
| `MRRC_FT8-Setup.exe` | 61 MB | `6a5e2f220e802704222cbada06b90e57efb10688e7dd9d0e652f368fdc0b1ff4` |

- Fast mirror (recommended in CN): <https://www.vlsc.net/mrrc_ft8/downloads/MRRC_FT8-Setup.exe>
- Versioned mirror: <https://www.vlsc.net/mrrc_ft8/downloads/MRRC_FT8-v1.2.0-Windows-x64-Setup.exe>
- GitHub repository: <https://github.com/cheenle/mrrc_ft8>

The v1.2.0 package was built on Windows 11 with Python 3.12.4, PyInstaller
6.21.0, and Inno Setup 6. All 909 tests passed; the frozen server boot,
HTTPS serving, and DSP/capture child spawns were smoke-verified on the
build VM. Radio/audio RF acceptance is an operator-side check on a physical
station. v1.2.0 adds rigctld lifecycle management to the launcher
restart, band-hunt/auto-call worked-set freshness and selected-state fixes,
and AUDIO false-positive re-verify.

## 系统要求

- Windows 11 或 Windows 10（64 位）
- 运行 Setup.exe 需要管理员权限（用于添加防火墙入站规则）
- 电台经 USB 串口（CAT）连接；音频用电脑声卡（或电台 USB 声卡）采样/输出

## 安装

1. 运行 `MRRC_FT8-Setup.exe`，按向导完成安装（默认装到 `C:\Program Files\MRRC_FT8`）。
   安装程序会创建：
   - 开始菜单快捷方式：`MRRC_FT8`
   - 可选桌面快捷方式
   - 开始菜单快捷方式：`Edit Configuration`（用记事本打开配置文件）
   - 防火墙规则 `MRRC_FT8 Web`（放行 TCP 8000 入站，便于局域网访问）
2. 从开始菜单或桌面快捷方式启动 `MRRC_FT8`。

## 首次运行

首次启动 launcher（一个黑色控制台窗口）会：

1. 在 `%LOCALAPPDATA%\MRRC-FT8\` 下生成配置 `ft8.env` 和数据目录
   （`data/device-config.json`、`certs/` 自签证书）。
2. 生成自签 HTTPS 证书，拉起 Hamlib `rigctld.exe`（串口 owner）和 `ft8-server.exe`
   （绑定 `0.0.0.0`，端口默认 **8000**）。
3. 健康检查通过后自动打开浏览器 `https://localhost:8000/desktop/`。

**首次会看到一个浏览器"连接不是私密连接/不受信任"警告**——这是自签证书的正常现象，
点一次"继续前往"即可（Chrome/Edge：高级 → 继续前往；Safari：显示详细信息 → 访问此网站）。
证书只在本机生成、不对外公开，这是可接受的。

### 默认密码

- 登录密码：**`abcd1234`**
- 修改方法见下文 §配置 → 修改密码。

## 使用

登录后就是 MRRC-FT8 的 Web 界面：解码/编码（Band Activity + RX/TX）、日志簿、
DXCC 等。界面既可用于本机，也可从局域网/远程的设备（手机、平板）打开。

### 配置电台与音频（Settings → Devices）

在界面的 **Settings → Devices** 里配置：

| 字段 | 说明 |
| ------ | ------ |
| Rig Model | 电台型号（下拉选择，含自定义型号） |
| CAT Serial Device | 串口号，下拉会列出本机所有 COM 口（`COM3` 等） |
| Baud Rate | 波特率（默认 38400） |
| Audio In / Audio Out | 输入（解码采样）/ 输出（音频）设备，从启动时枚举的设备列表里选 |

填好后先 **Save** 保存到 `%LOCALAPPDATA%\MRRC-FT8\data\device-config.json`，
再点 **Apply** —— 会经 `restart.ps1` 重启 rigctld + 服务器使配置生效
（重启日志在 `%LOCALAPPDATA%\MRRC-FT8\restart.log`）。

找不到正确的音频设备时：启动 launcher 时窗口里会打印设备枚举信息；界面下拉里出现的
名称与系统"声音设置"里的设备名一致。若电台通过 USB 声卡接入，通常选
`USB Audio Device` 一类名称的条目。

### 局域网 / 远程访问

同一局域网内，用别的电脑/手机浏览器访问：

```
https://<这台电脑的IP或主机名>:8000/desktop/
```

- **必须用 HTTPS**：浏览器对 HTTP + 局域网 IP 视为非 secure context，会禁用
  AudioWorklet 和麦克风（音频/PTT 全废）。因此本包默认走自签 HTTPS，
  远程设备第一次访问接受一次证书警告即可。
- 安装时的防火墙规则已放行 TCP 8000 入站；若改了端口，需要手动加规则
  （或 launcher 会在启动时尽力帮你加）。
- 远程访问也要用 `abcd1234` 登录（同一套密码）。

## 配置文件 `ft8.env`

配置文件在：

```
%LOCALAPPDATA%\MRRC-FT8\ft8.env
```

用开始菜单的 `Edit Configuration` 快捷方式即可打开。常用项：

```ini
MRRC_FT8_WEB_HOST=0.0.0.0
MRRC_FT8_WEB_PORT=8000
MRRC_FT8_MY_CALL=N0CALL
MRRC_FT8_MY_GRID=AA00AA
MRRC_FT8_PASSWORD_HASH=$argon2id$v=19$m=65536,t=3,p=4$...   # 登录密码哈希
# MRRC_FT8_ALLOWED_HOSTS=localhost,<主机名>,<局域网IP>       # 默认自动计算，一般不用动
```

改完重启 `MRRC_FT8` 生效。

### 修改密码

默认密码 `abcd1234` 对应的 Argon2id 哈希已写入 `ft8.env`。要改密码：

1. 在一台装有 Python 的机器上（或本仓库的 venv 里）执行：

   ```bash
   python -m server.main --hash-password <新密码>
   ```

   输出一串 `$argon2id$...` 哈希。
2. 把 `ft8.env` 里的 `MRRC_FT8_PASSWORD_HASH` 替换为新哈希，重启应用。

## 已知限制

- **TX 音频无法在 KVM 虚拟机上验证**：等时 USB 音频的 OUT 调度在虚拟机里有硬伤，
  想确认 TX 实际发射，请使用**实体 Windows 电脑**。
- 默认端口 8000 被占用时，改 `ft8.env` 的 `MRRC_FT8_WEB_PORT` 后重启。

## 故障排查

| 现象 | 原因 | 处理 |
| ------ | ------ | ------ |
| 浏览器打不开 `https://localhost:8000` | 服务器没起来，或端口被占 | 看 launcher 窗口输出；`MRRC_FT8_WEB_PORT` 换个端口 |
| 登录失败 | 密码错 / 改过 hash 没重启 | 用 `abcd1234`，或核对 `ft8.env` 的 `MRRC_FT8_PASSWORD_HASH` |
| 显示"连接不是私密连接" | 自签证书（正常） | 点"继续前往"接受一次 |
| 电台状态不更新 / 连不上 | CAT 串口号不对或驱动未装 | Settings → Devices 里把 `CAT Serial Device` 改成 Device Manager 里的实际 COM 口；装电台串口驱动 |
| 换配置后没生效 | 只 Save 没 Apply | 点 **Apply**（会重启 rigctld + 服务器） |
| 局域网设备连不上 | 防火墙规则缺失 / 端口已改 | 以管理员重跑 Setup.exe 补规则，或手动 netsh 加 TCP 8000 入站规则 |
| 关掉 launcher 控制台窗口后服务停止 | launcher 是控制台应用 | 用 **Ctrl-C** 优雅退出；想常驻可用隐藏启动方式（见打包手册） |
| 无解码 / 无音频 | 音频输入设备选错 | Settings → Devices 里把 Audio In 选成电台/实际声卡对应的设备 |

## 构建安装包（供开发者）

构建需要 Windows x64 + Python 3.11/3.12 + Node.js + PyInstaller + Inno Setup，
在 Win11 KVM 虚拟机上执行 `packaging/windows/build.ps1`。完整流程（含一次性
DSP/MinGW 编译、WSJT-X 运行时 DLL 与 Hamlib 装配）见 [`win_pack.md`](../win_pack.md)。

### 构建产物

```
dist\windows\MRRC_FT8\          组装好的应用目录
dist\windows\MRRC_FT8-Setup.exe  安装包
```

| 组件文件 | 用途 |
| ---------- | ------ |
| `MRRC_FT8.exe` | launcher：生成配置/证书，拉起 rigctld + 服务器，打开浏览器 |
| `ft8-server.exe` | FastAPI 服务器（含 DSP、web/desktop UI 资源） |
| `hamlib\rigctld.exe` | Hamlib 串口控制（唯一串口 owner） |
| `windows\restart.ps1` | Settings→Devices Apply 时重启 rigctld + 服务器 |
