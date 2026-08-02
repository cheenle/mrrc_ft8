# 2. Business Direction (BUS 411)

## 2.1 Vision

让连接家中电台的常驻主机可靠地承担 FT8/FT4 DSP 与硬件控制，操作者用手机横屏浏览器完成安全、清晰、低延迟的远程通联。

## 2.2 Goals

| ID | Goal | Measure |
|---|---|---|
| BG1 | 手机完成普通 FT8 QSO | SC2 |
| BG2 | 保留 WSJT-X Improved 解码能力 | SC1, SC3 |
| BG3 | 公网访问不牺牲发射安全 | SC4, SC5, SC6 |
| BG4 | 双平台服务化部署 | SC9, SC10 |
| BG5 | 操作界面保留 WSJT-X 高频核心 | SC7 |

## 2.3 User Scenarios

- 在家庭内或公网远程观察瀑布、时隙与解码候选。
- 人工选择 CQ 或目标台，交给 sequencer 完成本次标准 QSO。
- 任一已认证设备在异常时紧急停止发射。
- 自动写入 QSO，浏览并导出 ADIF。
- 查看本机健康、日志与原始诊断包。

## 2.4 Strategy

- 复用 WSJT-X 3.0.2 Improved 算法，不重新实现 FT8 编解码。
- 用独立 DSP Worker 隔离 Fortran/OpenMP 全局状态和崩溃面。
- 将 PTT、音频和租约留在主进程，DSP 无权发射。
- 采用 qFT8 式横屏空间模型，但保留 WSJT-X 操作语义并增加公网安全层。
- 首版只做普通 QSO，隐藏所有未实现模式与假控件。
- 以真实 macOS 电台验收为主，Linux 用可重复的模拟验收建立可移植性。

## 2.5 Relationship to Legacy MRRC

MRRC-FT8 是独立系统。音频与 CAT 资源不能被两个服务同时占用；`rigctld` 仍是串口唯一 owner。

## 2.6 Non-goals

首版不提供连续自动选台、Contest、Fox/Hound、SuperFox、WSPR、Q65、VHF/EME、Echo、Full Duplex、QSY Creator、原生手机应用、Docker 硬件部署或第三方遥测。

