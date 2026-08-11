# Windows 11/10 安装包（MRRC_FT8）设计

日期：2026-08-12
状态：已确认

## 目标

为 MRRC-FT8 整台站做一个 Windows 11/10 安装包 `MRRC_FT8-Setup.exe`，模式完全镜像
`mrrc_ft710` 的 Windows 打包方案（PyInstaller + launcher + Inno Setup，在 Win11 KVM
虚拟机上构建）。安装包包含：服务器 + DSP 解码/编码库 + rigctld + 移动 PWA +
desktop 客户端。装到 Windows 机上直接运行，控制本机连接的电台，支持局域网/远程访问。

## 已确认决策

| 项 | 决策 |
|---|---|
| 打包范围 | 整台站（服务器 + DSP + rigctld + web/desktop UI） |
| 应用名 | `MRRC_FT8` |
| 版本 | 1.1.0（对齐 `pyproject.toml`） |
| 首次运行密码 | 固定 `abcd1234`，Argon2id 哈希写入配置（`MRRC_FT8_PASSWORD_HASH`） |
| DSP 落地 | 在构建机上用 MinGW-w64 gfortran 从本项目 `dsp/` 源码编译 `wsjt_core.dll`；运行时 DLL（libgfortran-5 / libgomp-1 / libfftw3f-3 / libquadmath-0 / libwinpthread-1）从官方 WSJT-X Windows 安装包抽取借用 |
| 访问方式 | 局域网/远程可访问：绑定 0.0.0.0 + 自签 HTTPS（首次浏览器警告一次） |
| 构建环境 | 复用 `mrrc_ft710` 的 Win11 KVM 虚拟机（ham.vlsc.net → 192.168.122.133） |
| 桌面客户端 | 构建机上 `npm run build` 产物 `dist/` 打包进服务器（gitignore，不入库） |

## 架构

```
Mac（本机 ft8 工作区） --ssh--> ham.vlsc.net --ssh--> win11 VM
                                                   ├─ 编译 dsp/ → wsjt_core.dll
                                                   ├─ npm run build → desktop/ft8web/dist
                                                   ├─ pyinstaller × 2（server + launcher）
                                                   └─ iscc → dist\windows\MRRC_FT8-Setup.exe
```

安装后运行：

```
MRRC_FT8.exe (launcher)
 ├─ 首次：生成 %LOCALAPPDATA%\MRRC-FT8\ft8.env（含密码 hash、host/port，port 默认 8000）
 │        自签证书到 %LOCALAPPDATA%\MRRC-FT8\certs\
 │        初始化数据目录（cty.dat、db、data/device-config.json）
 ├─ 拉起 rigctld.exe（打包的 Hamlib，串口唯一 owner）
 ├─ 拉起 ft8-server.exe（绑定 0.0.0.0，HTTPS）
 └─ 健康检查通过后打开浏览器 https://localhost:<port>/desktop/
```

## 组件

### 1. 构建环境（一次性，VM 上）

- msys2/MinGW-w64 gfortran + CMake + FFTW3F 单精度开发库（编译 wsjt_core.dll）
- Node.js LTS（desktop 客户端 build）
- PyInstaller（`requirements-build.txt` 锁版，参考 mrrc_ft710 `pyinstaller==6.21.0`）
- Hamlib Windows 版 rigctld.exe + 其 DLL（下载归档到 `vendor/hamlib/`）
- 官方 WSJT-X 3.0.2 Windows 安装包 → 抽取 bin DLL 归档到 `vendor/wsjtx-win-runtime/`
- Inno Setup 6（已装）

### 2. DSP 库

- VM 上 `cmake -S dsp -B dsp/build` + build，用 MinGW gfortran 产出 `wsjt_core.dll`
- CMake 已覆盖 Win32（非 APPLE/UNIX 分支，无导出 map——对 Windows 无碍）
- `server/core/worker.py::default_library_path` 增加 `.dll` 分支（`sys.platform == "win32"`）
- 打包时把官方 WSJT-X 的运行时 DLL 与 `wsjt_core.dll` 放同一目录

### 3. 服务器改动（本项目唯一业务代码改动）

`server/main.py`：
- `uvicorn.run` 的 host/port 从环境变量读取（`MRRC_FT8_WEB_HOST` 默认 `127.0.0.1`、
  `MRRC_FT8_WEB_PORT` 默认 `8000`），替换硬编码
- 新增 `--ssl-cert` / `--ssl-key` 参数 → uvicorn `ssl_keyfile` / `ssl_certfile`
- 新增 `MRRC_FT8_ALLOWED_HOSTS` 支持已在（默认 `localhost`）——绑定 0.0.0.0 后需加
  host 校验，允许配置 `0.0.0.0` 时放行

新增 `windows/ssl_bootstrap.py`（从 mrrc_ft710 移植）：自签证书生成到用户数据目录。

冻结模式路径处理（关键）：
- `server/main.py::_static_dir` → `_internal/server/web/static`
- `server/main.py::_desktop_dist_dir` → `_internal/desktop/ft8web/dist`
- `server/engine/dxcc.py::get_cty_database` → `_internal/cty.dat`
- `server/engine/repository.py` 的 db/pending 相对 cwd → launcher 把 cwd 设为可写数据目录，
  或用 `MRRC_FT8_DB_PATH` / `MRRC_FT8_PENDING_PATH` 指向用户数据目录
- 用 `sys.frozen` 分支 + `sys.executable`/`_MEIPASS` 解析应用根目录的辅助函数

### 4. 启动器 `windows/launcher.py`（新文件，参考 mrrc_ft710 移植）

- `user_data_dir()` → `%LOCALAPPDATA%\MRRC-FT8`
- 首次运行：从 `windows/default.env` 生成 `ft8.env`（写入密码 hash、host/port），
  生成自签证书，初始化数据文件（cty.dat 拷贝、db 空文件、`data/` 目录）
- `start_rigctld()`：用 `data/device-config.json`（或 env 回退）拉起打包的
  `rigctld.exe`（参数同 macOS `restart.sh` 的 rigctld 启动行）
- `build_server_command()`：`ft8-server.exe --ssl-cert ... --ssl-key ...`
- 健康检查（`/api/health`）→ `webbrowser.open(https://localhost:<port>/desktop/)`
- 添加 Windows 防火墙入站规则（端口放行）
- Ctrl-C / 关窗 → 停服务器 + rigctld

`windows/default.env`（参考 mrrc_ft710）：`MRRC_FT8_PASSWORD_HASH`、`MRRC_FT8_MY_CALL`、
`MRRC_FT8_MY_GRID`、`MRRC_FT8_WEB_HOST=0.0.0.0`、`MRRC_FT8_WEB_PORT`、`MRRC_FT8_RIGCTLD=127.0.0.1:4532`、
音频设备等。

### 5. PyInstaller specs（`packaging/windows/`）

- `ft8_server.spec`：Analysis 入口 `server/main.py`；datas 含
  `server/web/static`、`desktop/ft8web/dist`（build 后）、`cty.dat`、
  `wsjt_core.dll` + WSJT-X 运行时 DLL、`rigctld.exe` + Hamlib DLL；
  hiddenimports 含 uvicorn 各 loop/protocol 模块（参考 mrrc_ft710）
- `ft8_launcher.spec`：launcher.py + `windows/default.env`
- `requirements-build.txt`：pyinstaller 锁版

### 6. Inno Setup `packaging/windows/MRRC-FT8.iss`

- `AppName=MRRC_FT8`、`AppVersion=1.1.0`、`DefaultDirName={autopf}\MRRC_FT8`
- `OutputBaseFilename=MRRC_FT8-Setup`
- 桌面/开始菜单快捷方式；[Run] 安装后启动
- `ArchitecturesAllowed=x64compatible`

### 7. 构建编排 `packaging/windows/build.ps1`

参考 mrrc_ft710 的 `build.ps1`（`Invoke-Checked` 闸门，$LASTEXITCODE 检查）：
1. `python -m pytest tests/`
2. cmake + build `dsp/` → `wsjt_core.dll` 拷贝到 app 目录
3. `cd desktop/ft8web; npm ci; npm run build`
4. pyinstaller × 2
5. iscc → `dist\windows\MRRC_FT8-Setup.exe`

### 8. 文档

- `win_pack.md`（打包方操作手册：环境拓扑、一次性准备、每次打包流程、验证、取回、发布、故障排查表）
- `docs/WINDOWS_INSTALLER_GUIDE.md`（用户向：安装、首次密码 abcd1234、局域网访问、
  HTTPS 证书警告说明、电台/音频配置走 `/api/v1/devices`）

## 验证

- VM 上 pytest 全绿
- 打包产物：`wsjt_core.dll` + 运行时 DLL 齐全、`_internal/static`、`_internal/desktop/ft8web/dist`、
  `cty.dat`、rigctld.exe 存在
- 冒烟：安装到 VM，launcher 拉起 rigctld + server，HTTPS 健康检查通过，
  `/desktop/` 登录后用 abcd1234 能进界面
- 真机音频/电台验收不在本设计范围（参考 mrrc_ft710 的 KVM TX 音质限制结论）

## 风险 / 已知坑

- **DSP MinGW 编译**是最大不确定项：FFTW3F 单精度 + OpenMP 在 MinGW 下需验证
  （msys2 包 `mingw-w64-x86_64-fftw` 提供单精度）；CMake `find_package(OpenMP)`
  / `pkg-config` 在 msys2 下可用
- **PyInstaller 冻结路径**：包内 `Path(__file__)` 定位需逐处核对，冒烟验证
- **rigctld Windows 二进制**：需从 Hamlib 官方 Windows 构建获取并验证 CAT 通
- **firewall**：launcher 提权添加防火墙规则可能触发 UAC（接受）
- 服务器 `dotenv` 加载 `.env`：launcher 注入 env 即可，无需 .env 文件

## 不做（YAGNI）

- 代码签名（无证书；分发靠 SHA-256，同 mrrc_ft710）
- 自动更新
- 跨平台打包矩阵（仅 Windows x64）
