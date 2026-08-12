# Windows 安装包打包流程（MRRC_FT8）

> 用途：在 ham.vlsc.net 上的 Win11 KVM 虚拟机中构建并冒烟验证 `MRRC_FT8-Setup.exe`。
> 打包模式完全镜像 [`mrrc_ft710/win_pack.md`](../mrrc_ft710/win_pack.md)（PyInstaller + launcher + Inno Setup）。
> 用户向的安装/使用说明见 [docs/WINDOWS_INSTALLER_GUIDE.md](docs/WINDOWS_INSTALLER_GUIDE.md)，本文是**打包方**的操作手册。
> 与 mrrc_ft710 的主要差异：本包要**在 VM 上用 MinGW gfortran 编译 DSP**（`wsjt_core.dll`），并打包 Hamlib `rigctld.exe`（串口 owner）；没有 FTDI/FT4222 频谱组件。

## 1. 环境拓扑

```
本机 Mac (ft8 工作区 /Users/cheenle/HAM/ft8)
   │  ssh ham.vlsc.net          （Ubuntu 24.04 KVM 宿主机，sudo 免密）
   ▼
ham.vlsc.net
   │  ssh cheenle@192.168.122.133  （libvirt 默认 NAT 网段，与 mrrc_ft710 同一台 VM）
   ▼
win11 虚拟机 (desktop-ssddf0b)
   C:\mrrc_ft8              源码工作区
   C:\Users\cheenle\*.ps1   辅助脚本（build_vm.ps1）
   dist\windows\            构建产物
```

- VM 管理：`sudo virsh -c qemu:///system list --all`（必须带 `qemu:///system` 且用 sudo）
- VM IP 查询：`sudo virsh -c qemu:///system domifaddr win11`
- VM 的 22 端口有 OpenSSH Server，默认 shell 是 **PowerShell 5.1**（写命令时注意，见 §5 坑列表）
- 本包不需要 USB 设备直通（FT8 是头less 服务器，电台经 rigctld 走本机串口；音频由本机声卡采样），VM 上冒烟用假串口/无电台即可

## 2. 一次性准备（按需执行，配好后跳 §3）

### 2.1 SSH 免密进 VM

Windows OpenSSH 对**管理员用户**只读 `C:\ProgramData\ssh\administrators_authorized_keys`（不是用户目录下的 `authorized_keys`），且 ACL 必须只有 Administrators/SYSTEM：

```powershell
Add-Content -Path C:\ProgramData\ssh\administrators_authorized_keys -Value '<宿主机的 ssh-ed25519 公钥>'
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

宿主机 ham.vlsc.net 的 `~/.ssh/id_ed25519` 公钥已加入，故从 Mac 可一条链路直连：
`ssh ham.vlsc.net` → `ssh cheenle@192.168.122.133`（宿主机上装有 `sshpass` 备用）。

### 2.2 VM 上的构建软件

- Python 3.12（系统级，`python` / `py` 均在 PATH；项目要求 >=3.11）
- Inno Setup 6：`C:\Program Files (x86)\Inno Setup 6\iscc.exe`（不在 PATH，`build_vm.ps1` 会临时加）
- Node.js LTS（desktop 客户端 `npm ci` / `npm run build` 用）
- 源码区 `C:\mrrc_ft8` 下建 venv，装齐依赖：

```powershell
cd C:\mrrc_ft8
python -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m pip install -r packaging\windows\requirements-build.txt   # pyinstaller==6.21.0 锁版 + cryptography + pyserial
```

### 2.3 DSP 编译（wsjt_core.dll）——本包独有的一次性步骤

用 msys2 的 MinGW-w64 gfortran 从 `dsp/` 源码编译 DSP 库（CMake 已覆盖 Win32，非 APPLE/UNIX 分支）：

```bash
# msys2 mingw64 shell 里
pacman -S mingw-w64-x86_64-gcc-fortran mingw-w64-x86_64-cmake mingw-w64-x86_64-fftw
cd /c/mrrc_ft8
cmake -S dsp -B dsp/build -G "MinGW Makefiles"
cmake --build dsp/build -j
cp dsp/build/wsjt_core.dll vendor/wsjtx-runtime/windows/x64/
```

**依赖**：`dsp/CMakeLists.txt` 引用 `../wsjtx-3.0.2/lib`，编译时工作区里必须有 `wsjtx-3.0.2/` 源码树（`README` 说它是只读 vendor；一次性 scp 上去即可，每期源码 zip 不包含它）。DSP 源码改动频繁才会重编，否则沿用 `vendor/wsjtx-runtime/windows/x64/wsjt_core.dll` 成品。

### 2.4 WSJT-X 运行时 DLL + Hamlib rigctld

**WSJT-X 运行时 DLL**：官方 WSJT-X 3.0.2 Windows 安装包装到 VM 后，把 `C:\Program Files\WSJT\bin\` 下的
`libgfortran-5.dll`、`libgomp-1.dll`、`libfftw3f-3.dll`、`libquadmath-0.dll`、`libwinpthread-1.dll`
拷到 `vendor/wsjtx-runtime/windows/x64/`（与 `wsjt_core.dll` 同目录）。`ft8_server.spec` 已把整个目录收进 `_internal\`。

**Hamlib rigctld**：从 Hamlib 官方 Windows 构建下载 `rigctld.exe` + 其 DLL（`libhamlib*.dll` 等），
放 `vendor/hamlib/windows/x64/`。`build.ps1` 会把整个目录拷到 `{app}\hamlib\`，launcher 从那里拉起。

这两个目录已加进 `.gitignore`（在 VM 上装配，不入 git）。

### 2.5 build_vm.ps1 辅助脚本（在 VM 上建一次）

与 mrrc_ft710 相同：Inno 目录入 PATH → 激活 venv → 调仓库里的 `packaging\windows\build.ps1`。

```powershell
# C:\Users\cheenle\build_vm.ps1
$ErrorActionPreference = "Stop"
$isccDir = "${env:ProgramFiles(x86)}\Inno Setup 6"
if (Test-Path "$isccDir\iscc.exe") { $env:Path = "$isccDir;" + $env:Path }
Set-Location C:\mrrc_ft8
.\venv\Scripts\Activate.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\build.ps1
Write-Host "BUILD_DONE"
```

## 3. 每次打包流程

### Step 0 — 本地检查

```bash
venv/bin/python -m pytest tests/ -q        # 必须全绿（2026-08-12 为 908 passed）
```

确认版本号一致：`pyproject.toml` 的 `version`、`packaging/windows/MRRC-FT8.iss` 的 `MyAppVersion`（当前均为 1.1.0）。

### Step 1 — 打源码包（在本机仓库根目录）

```bash
mkdir -p dist && rm -f dist/mrrc_ft8_src.zip
zip -qr dist/mrrc_ft8_src.zip . \
  -x "./.git/*" "./venv/*" "./.venv/*" "./dist/*" "./build/*" \
     "./wsjtx-3.0.2/*" "./mrrc-ft8.db" "./data/*" "./__pycache__/*" \
     "./windows/__pycache__/*" "./tests/__pycache__/*" "./.pytest_cache/*" \
     "./.claude/*" "./.superpowers/*" "./*.pyc" "./.DS_Store" \
     "./desktop/ft8web/node_modules/*" "./desktop/ft8web/dist/*"
```

**关键**：
- `./.agents/*` **不能排除**——`tests/test_sdd_harness.py` 依赖其中的 harness 文件（同 mrrc_ft710 的教训，缺了 VM 上测试会失败）。
- `./data/*` 必须排除（含本机运行时 `device-config.json`、`mrrc-ft8.db`、日志）。
- `wsjtx-3.0.2/`（98M 只读 vendor）和 `mrrc-ft8.db`（12M）必须排除。
- `vendor/` **保留在包内**（内含装配好的 `wsjt_core.dll` + 运行时 DLL + rigctld），这样 VM 上解压即用、且经得起 §3 Step 3 的整体删除。

### Step 2 — 上传到 VM（经 ham 跳板）

```bash
scp dist/mrrc_ft8_src.zip ham.vlsc.net:/tmp/
ssh ham.vlsc.net "scp /tmp/mrrc_ft8_src.zip cheenle@192.168.122.133:mrrc_ft8_src.zip"
```

### Step 3 — VM 上解压

```bash
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -Command \"Set-Location C:\Users\cheenle; if (Test-Path C:\mrrc_ft8) { Remove-Item C:\mrrc_ft8 -Recurse -Force }; Expand-Archive mrrc_ft8_src.zip -DestinationPath C:\mrrc_ft8\"'"
```

**注意**：Step 3 的 `Remove-Item C:\mrrc_ft8` 会把 VM 上的 `venv` 一起删掉（zip 不含 venv）——删过目录就必须重跑 §2.2 的**完整四条**（含 `python -m venv venv`）再构建。若删除时报文件被占用，是 VM 上残留的服务进程持锁，先 `Stop-Process`（见 §5 表）。

### Step 4 — 构建

远程执行（约 3-5 分钟：908 个测试 → npm build → PyInstaller ×2 → iscc）：

```bash
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\cheenle\build_vm.ps1'"
```

`build.ps1` 有 `Invoke-Checked` 闸门：测试或任何一步非零退出都会中止，不会带病出包。

### Step 5 — 验证产物（在 VM 上）

```powershell
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\MRRC_FT8.exe
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\ft8-server.exe
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\_internal\wsjt_core.dll          # DSP 库
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\_internal\libfftw3f-3.dll        # 运行时 DLL 与 wsjt_core.dll 同目录
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\_internal\server\web\static\index.html
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\_internal\desktop\ft8web\dist\index.html
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\_internal\cty.dat
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\hamlib\rigctld.exe
dir C:\mrrc_ft8\dist\windows\MRRC_FT8\windows\restart.ps1
Get-FileHash C:\mrrc_ft8\dist\windows\MRRC_FT8-Setup.exe -Algorithm SHA256
```

### Step 6 — 取回本机

```bash
ssh ham.vlsc.net "scp cheenle@192.168.122.133:C:/mrrc_ft8/dist/windows/MRRC_FT8-Setup.exe /tmp/MRRC_FT8-Setup.exe"
scp ham.vlsc.net:/tmp/MRRC_FT8-Setup.exe dist/windows/
shasum -a 256 dist/windows/MRRC_FT8-Setup.exe
```

### Step 7 — 安装 + 冒烟（在 VM 桌面，手动）

装 Setup.exe → 启动 `MRRC_FT8.exe`（launcher，控制台窗口），确认：

1. `%LOCALAPPDATA%\MRRC-FT8\ft8.env` 生成，含 `MRRC_FT8_PASSWORD_HASH`（对应 `abcd1234`）与 `MRRC_FT8_WEB_PORT=8000`；
2. 控制台打印自签证书提示，浏览器打开 `https://localhost:8000/desktop/`，接受一次"不受信任"警告（Chrome/Edge: 高级 → 继续前往）；
3. 用 `abcd1234` 登录成功；
4. `https://<vm-lan-ip>:8000/desktop/` 从宿主机也能开（验证绑定 0.0.0.0 + `MRRC_FT8_ALLOWED_HOSTS` + 防火墙规则）；
5. `/api/v1/health` 报告 radio/audio 正常（无电台时 radio 红是预期的）；
6. UI 里 Settings → Devices 保存一次配置并 Apply，`restart.ps1` 能重启 rigctld + server（日志 `%LOCALAPPDATA%\MRRC-FT8\restart.log`）。

**已知限制**（同 mrrc_ft710 实测定论）：这台 KVM VM 的等时 USB 音频 OUT 调度有硬伤，**TX 音频无法在这台 VM 上验证**——TX 音频验收请到实体 Windows 机做。

## 4. 发布到网站（可选，等镜像站点就绪）

参照 mrrc_ft710：下载镜像在 **www.vlsc.net**，webroot `/var/www/vlsc.net/mrrc_ft8/`。发布时把
`MRRC_FT8-Setup.exe` 拷到 `website/downloads/`，跑 `./deploy.sh`（备份 + 上传 + `nginx -t`），
并同步更新版本号/大小/SHA-256 的地方：`website/index.html`、`website/zh/index.html`、`docs/WINDOWS_INSTALLER_GUIDE.md`、`README.md`、`CHANGELOG.md`。

## 5. 故障排查（mrrc_ft710 踩过 + 本包特有）

| 现象 | 原因 | 处理 |
|------|------|------|
| `virsh list` 看不到 win11 | 默认连 qemu:///session | `sudo virsh -c qemu:///system list --all` |
| 公钥加了仍 Permission denied | 管理员用户只认 `C:\ProgramData\ssh\administrators_authorized_keys` | 见 §2.1，注意 icacls 权限 |
| SSH 里 `&&` 报错 | VM 默认 shell 是 PowerShell 5.1 | 用 `;` 或把命令写成 .ps1 scp 上去执行 |
| PS 远程命令引号地狱 | 多层 ssh 转义 | 本地写脚本 → scp → 远程执行；或 `powershell -EncodedCommand <UTF16LE-Base64>` |
| 测试输出 grep 不到内容 | PowerShell `2>` 重定向写 UTF-16LE | `iconv -f UTF-16LE -t UTF-8` 后再处理 |
| 测试失败但 build 继续 | `$ErrorActionPreference` 不管原生命令 | build.ps1 已修：`Invoke-Checked` 检查 `$LASTEXITCODE` |
| VM 上测试 UnicodeDecodeError | 虚拟机是 GBK(c936) 中文区域，测试 `read_text()` 未指定编码 | 测试统一 `encoding="utf-8"`，harness 子进程设 `PYTHONIOENCODING=utf-8` |
| VM 上 harness 测试失败 | 源码 zip 误排 `.agents/` | 见 Step 1 关键提示（`./.agents/*` 保留） |
| Step 3 删目录后 build_vm.ps1 报 `.\venv\Scripts\Activate.ps1` 找不到 | Step 3 的 `Remove-Item` 把 venv 一起删了（zip 不含 venv） | 重跑 §2.2 完整四条（含 `python -m venv venv`）再构建 |
| Step 3 删除时报文件被占用 | VM 上残留服务进程持锁 | `Get-Process` 找到并 `Stop-Process` 对应 python/powershell 再解压 |
| `wsjt_core.dll` 打不进包 | vendor 里缺 DLL | 重跑 §2.3 编译并拷到 `vendor/wsjtx-runtime/windows/x64/` |
| 装完启动报 rigctld 找不到 | `vendor/hamlib/windows/x64` 缺 rigctld.exe | build.ps1 只警告不中止；补 DLL 后重打 |
| 浏览器连不上 8000 | 防火墙没放行 / UAC 拒绝了 netsh | 安装时以管理员运行 Setup.exe（[Run] 段加规则）；launcher 首启也会再试一次 |
| 装新包后 8000/串口行为异常 | 旧服务实例还活着占着端口/串口 | `Get-Process -Name ft8-server,rigctld | Stop-Process -Force` 后重启动 |
| **TX 音频在这台 KVM VM 上必然有问题，无法做 TX 验收** | KVM 等时 USB 音频 OUT 调度硬伤（同 mrrc_ft710） | TX 音频验收请到实体 Windows 机做 |

## 6. 常用命令速查

```bash
# VM 状态
ssh ham.vlsc.net "sudo virsh -c qemu:///system list --all"

# 一条命令进 VM 跑 PowerShell
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -Command \"<cmd>\"'"

# VM 上跑测试
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'cd C:\mrrc_ft8; venv\Scripts\python.exe -m pytest tests/ -q'"

# 构建
ssh ham.vlsc.net "ssh cheenle@192.168.122.133 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\cheenle\build_vm.ps1'"

# 健康检查（HTTPS，8000；登录拿 cookie 再访问 /api/v1/health）
#   $s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
#   Invoke-RestMethod -Uri https://192.168.122.133:8000/api/v1/session/login -Method Post -ContentType "application/json" -Body (@{password="abcd1234"} | ConvertTo-Json) -WebSession $s -SkipCertificateCheck
#   Invoke-RestMethod -Uri "https://192.168.122.133:8000/api/v1/health" -WebSession $s -SkipCertificateCheck
# （VM 是 PowerShell 5.1，无 -SkipCertificateCheck 时先执行 [System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}）
```
