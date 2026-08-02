# MRRC-FT8 — Headless FT8/FT4 Server

WSJT-X 3.0.2 (Improved fork) DSP Worker + Python FastAPI 服务器 + 横屏移动 Web 远程。
设计记录见 `SDD/`（TeamSD 15 章）；旧 mrrc_ft710 设计归档于 `SDD-legacy-mrrc_ft710/`。

## 构建与运行

```bash
# 1. DSP 核心库（需要 gfortran-mp-13 + fftw3f）
cmake -S dsp -B dsp/build -DCMAKE_Fortran_COMPILER=gfortran-mp-13
cmake --build dsp/build -j

# 2. Python 环境
python3 -m venv venv && venv/bin/pip install -e '.[dev]'

# 3. 运行
OMP_STACKSIZE=10M venv/bin/python -m server.main
```

WSJT-X Improved 的 `sync8var` 在线程栈上分配大型工作数组；必须沿用
upstream GUI 的 `OMP_STACKSIZE=10M`。生产 Worker 启动代码须在首次导入
NumPy/SciPy、加载 `wsjt_core` 或触发 OpenMP runtime 前执行
`os.environ.setdefault("OMP_STACKSIZE", "10M")`，不可在 runtime 已加载后补设。

## 测试

```bash
venv/bin/python -m pytest tests/
```

## 模块表

| 路径 | 职责 |
|---|---|
| `dsp/` | Fortran shim + CMake → `wsjt_core` 共享库（FT8/FT4 解码/编码） |
| `dsp/ft8_stdcall.f90` | 从 WSJT-X `lib/qra/q65/q65_set_list.f90:66-97` 等价提取的标准呼号判定；隔离无关 Q65 链；由 `test_ft8_encode.py` fresh-build 回归 |
| `dsp/cmake/improved-ft8.cmake` | Improved `ft8var` 的显式、无 glob 最小源清单 |
| `dsp/wsjt_partition.f90` | 将合法整数频点精确、无重叠地分配到 1–12 个相邻 OpenMP 分带 |
| `dsp/wsjt_a8_gate.f90` | 以 OpenMP atomic 管理普通解码命中 Rx 附近时的 A8 请求门控 |
| `dsp/wsjt_improved.f90` | Improved profiles 0–4、确定性 A8 owner、严格 OpenMP team 校验和纯 Fortran batch 调度 |
| `dsp/wsjt_test_hooks.f90` | 仅 `MRRC_FT8_TEST_HOOKS=ON` 时编译的非生产 direct-A8 测试入口 |
| `server/core/` | DSP Worker/supervisor；ctypes 绑定 wsjt_core；全局 DSP lock |
| `server/engine/` | 编排器（UTC 时隙）、音频 RX/TX、rig（rigctld）、sequencer、TX 链路（dsp_encode 编码、tx_driver 时隙奇偶泵、cq_loop 自动 CQ 循环、qso_log 落库助手）、ADIF |
| `server/web/` | FastAPI REST/WS + 移动 PWA 静态资源 |
| `deploy/` | Caddyfile（模板+live 实例）、Caddy root LaunchDaemon、systemd unit、macOS LaunchAgent（密码哈希经 `python -m server.main --hash-password` bootstrap） |
| `acceptance/` | 硬件验收脚本（FT-710 real-radio：preflight/monitor/`--tx`，不进 pytest） |
| `wsjtx-3.0.2/` | vendor 参考源码（只读，禁止修改） |
| `tests/` | pytest；ft8sim/ft4sim 合成信号回归 |

## 铁律（constraints.json 强制）

- 进解码器音频恒为 **12 kHz int16 单声道**；TX 波形恒为 48 kHz。其他采样率禁止进 DSP。
- 所有 DSP 调用必须经 `server/core/binding.py` 的全局锁（packjt77 全局状态）。
- DSP 只能在独立 Worker 中运行；OpenMP 线程只在 Fortran 聚合结果，禁止回调 Python。
- TX 只能经 sequencer + PTT watchdog；禁止阻塞式 PTT 确认循环。
- UTC 时隙对齐一律 `floor(epoch / TRperiod)`。
- 串口唯一 owner 是 rigctld；任何模块不得直接 open 串口设备。
- 公网入口仅 Caddy 80/443；FastAPI/rigctld 只监听本机；WebSocket 禁止 URL token。
- 多会话只有一个控制租约；任何已认证会话均可紧急 STOP TX。
- 不修改 `wsjtx-3.0.2/` 内任何文件；补丁副本放 `dsp/patched/` 并在此登记。
- `dsp/ft8_stdcall.f90` 是 headless 最小链接适配，不属于 Improved 算法补丁；必须与上述 vendor 行保持等价，禁止扩展为 Q65 依赖。

## Vendor 补丁副本登记

| 本地文件 | Origin / revision | 唯一差异 | 原因 | 回归 |
|---|---|---|---|---|
| `dsp/patched/encode174_91var.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/encode174_91var.f90` | 一处 `include '/lib/ft8/ldpc_174_91_c_generator.f90'` 改为相对 include | relocatable headless build | `test_vendor_policy.py` 逆替换 byte-identical + Improved synthetic profiles |
| `dsp/patched/osd174_91var.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/osd174_91var.f90` | 相对 LDPC include；将 `first_osd` 检查完整置于 named critical 内 | relocatable build；并发只初始化一次生成矩阵 | 精确正/逆变换 + 并发 profile stress |
| `dsp/patched/four2avar.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/four2avar.f90` | 相对 FFTW include；plan registry 设为 `THREADPRIVATE` | relocatable FFTW；线程私有 plan/address cache | 精确正/逆变换 + 重复并发 region stress |
| `dsp/patched/ft8_mod1.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/ft8_mod1.f90` | `dd8` 设为 `THREADPRIVATE` | 每个分带从同一 slot 副本独立、确定性 subtraction | 精确正/逆变换 + 多信号单/多线程集合等价 |
| `dsp/patched/ft8_decodevar.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/ft8_decodevar.f90` | cycle 2/3 scratch 改为每线程；A8 使用确定性 owner、barrier 和 atomic gate snapshot | 消除共享 slot/cycle/A8 竞态 | 精确正/逆变换 + team/A8/cycle 并发回归 |
| `dsp/patched/ft8_downsamplevar.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/ft8_downsamplevar.f90` | saved FFT cache `cxx` 设为 `THREADPRIVATE` | 防止各频带下采样缓存互相覆盖 | 精确正/逆变换 + 重复并发 region stress |
| `dsp/patched/ft8apsetvar.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/ft8apsetvar.f90` | 每请求清零并无条件重建 AP masks | 防止 call/grid/AP 上下文跨请求泄漏 | 精确正/逆变换 + 同 CDLL 上下文切换回归 |

七个副本禁止增加登记外差异；回归会验证每项替换恰好一次、逆替换后与
vendor byte-identical，并另行验证完整 vendor tree digest。FFTW plan cache
与 DSP Worker 同寿命，在进程退出时由操作系统统一回收。

## 编码约定

- Python：asyncio；硬件 I/O 一律 `asyncio.to_thread`；类型标注；Google 风格 docstring。
- Fortran shim：普通 subroutine + `bind(C)` 命名；不改 wsjtx 原有源码逻辑。
- 前端：无构建步骤的 vanilla JS，`static/` 下模块化；`index.html` 无内联 JS 逻辑。
- 每次代码变更同步 SDD 对应章节 + `SDD/14-version-history.md`（见 sdd-guardian skill）。
