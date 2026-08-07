# 设计：new-dxcc-auto-call 页面全面更新至 SDD V1.5 + 全 SVG 配图

日期：2026-08-08 · 状态：已批准（用户两次确认）

## 目标

将 `https://www.vlsc.net/mrrc_ft8/zh/new-dxcc-auto-call.html`（源：
`docs/new-dxcc-auto-call.md`，构建：`website/build_new_dxcc_doc.py`）全面更新至
SDD V1.5 现状，并把页面所有图示 SVG 化（全面配图，13 张），完成后经
`website/deploy.sh` 发布到生产。

## 背景

页面写于 SDD V1.3（commit 42ca97f）。此后新增两个实质变更：

- **V1.4（2026-08-07）serial-owner guard**：现场一台残留旧 mrrc_ft710 `server.py`
  直接 open FT-710 CAT 串口与 rigctld 争抢 4 小时（~90% rig 轮询超时，band_hunt
  切频 5 次失败）。`restart.sh` 新增 `serial_guard()`：启动 rigctld 前检测非
  rigctld 持有者，冲突 fail-fast；`MRRC_FT8_SKIP_SERIAL_GUARD=1` 应急跳过（AD-008）。
- **V1.5（2026-08-08）band-hunt 实体名归一化**：`rank_bands` worked 过滤直接拿
  pskreporter 普通名对 cty 规范名失配（Germany/Malaysia/Turkey ↔ Fed. Rep. of
  Germany/West Malaysia/Asiatic Turkey；42 个实体中仅这 3 个不一致）→ 已通联实体
  被判 new → band_hunt 反复切 15m 追 Germany（qso 表已有 64 条 DL/DK）。修复：
  `_CTY_NAME_ALIASES` + `_canonical_entity_name()`；auto-call 的 `is_new_dxcc`
  不受影响（两侧都用 cty.lookup）。

页面现有 2 处 ASCII 图（§2 端到端链路、§3.6 QSO 状态机）。

## 方案：构建时内联 SVG（方案 A，已选定）

SVG 源文件放 `website/images/`，Markdown 用 `![图题](../images/x.svg)` 引用；
`build_new_dxcc_doc.py` 在 pandoc 转换后把指向本地图的 `<img>` 替换为 SVG 文件
内容，外包 `<figure class="doc-fig"><figcaption>`。

优点：内联 SVG 继承页面 CSS 变量（`var(--accent)` 等）与 Inter/JetBrains Mono
网络字体，主题完全一致；Markdown 保持干净；单文件 HTML 部署原子。
否决：外部 `<img>`（沙盒内无法用页面字体/变量）；Markdown 内联 raw HTML
（文档膨胀不可维护）。

## 内容更新（docs/new-dxcc-auto-call.md）

1. 头部适用版本 → **SDD V1.5（2026-08-08）**，括注三批修复。
2. §3.8 band_hunt：新增 V1.5 实体名归一化段落（根因/修复/auto-call 不受影响）。
3. §4 配置表：新增 `MRRC_FT8_SKIP_SERIAL_GUARD` 行（默认启用守卫，=1 应急跳过）。
4. §6 安全不变量 #7：扩展 serial_guard 部署期守卫说明。
5. §7 现场案例复盘：重组为三个案例——
   - 7.1 TN8GD 频率纪律（现有内容，保留数据与复盘）；
   - 7.2 CAT 串口争抢与 serial_guard（V1.4 现场：18:01 起 4 小时、数千条
     wrong reply/Rig busy、band_hunt 5 次切频失败、停掉残留进程即恢复）；
   - 7.3 Germany 实体名失配与归一化（V1.5 现场：12h 复盘）。
6. §8 已知限制：新增别名表仅覆盖 3 个已知失配、未知名原样通过不误伤。
7. 两处 ASCII 图替换为图引用；各小节按配图清单插图。

## 配图清单（13 张，全部 SVG）

| 图 | 节 | 内容 |
|---|---|---|
| 图1 auto-call-pipeline.svg | §2 | 端到端链路 8 段纵向流水线 |
| 图2 capture-decode.svg | §3.1 | FT-710→capture_proc→UtcRing→DSP Worker→on_decode；12 kHz/48 kHz 铁律、进程边界标注 |
| 图3 dxcc-lookup.svg | §3.2 | cty.dat 三规则 lookup（=精确/(数字)/最长前缀 trie）+ worked 集比较 → is_new_dxcc |
| 图4 decision-gate.svg | §3.3 | auto_call_candidate 六条件 AND 门（每 slot 至多第一个） |
| 图5 arm-interlock.svg | §3.4 | safety.arm 互锁矩阵 CAT/audio/DSP/clock；TxRefused→skip/下槽重试；成功→reply_to+audit |
| 图6 tx-chain.svg | §3.5 | TxDriver 奇偶泵 + I9 5.0s 窗口 + fit guard → dsp_encode 48 kHz → safety.transmit → rigctld |
| 图7 qso-states.svg | §3.6 | 应答侧状态机 REPLYING→REPORT→ROGER_REPORT→ROGERS→SIGNOFF/DONE + Tx1–Tx5 + 伙伴触发 + PARTNER_LOST/RETRY_EXHAUSTED 出口 |
| 图8 persist-worked.svg | §3.7 | _ensure_log→on_qso→enqueue→record_qso→dxcc_dirty→cache 重建→worked 闭环 + qso-pending.jsonl 崩溃恢复 |
| 图9 band-hunt-loop.svg | §3.8 | 双闸门→轮询 /api/band_hunt→V1.5 归一化→rank/decide→rig 切频→解码→auto-call 闭环 |
| 图10 safety-gate.svg | §6 | 单点 TX 门：Web/sequencer/auto-call → SafetyController → 互锁 → PTT；STOP 优先；不自动恢复 |
| 图11 tn8gd-frequency.svg | §7.1 | 频率轴：CQ@843；旧 TX@1500（×）；UC-003 后 TX@843（✓）；自身回波@1500 |
| 图12 serial-guard.svg | §7.2 | 残留进程与 rigctld 共持串口→字节争抢→serial_guard fail-fast 门 |
| 图13 entity-normalize.svg | §7.3 | pskreporter 名→别名表→cty 规范名映射；worked 判定修复 |

## 视觉设计系统（13 图统一）

- 画布：`var(--bg-tertiary,#10161e)` 卡片 + `var(--border,rgba(255,255,255,0.06))`
  描边；节点 `#0d1117` 圆角矩形 r=8，描边 1.5px。
- 语义色：青 `var(--accent,#22d3ee)` 主流程/强调；琥珀 `#f59e0b` 故障/跳过/重试；
  绿 `#34d399` 成功/落库；红 `#f87171` 错误/冲突；灰 `#8899aa`/`#5c6370` 注释。
- 字体：`Inter, system-ui, sans-serif` 正文；`'JetBrains Mono', ui-monospace, monospace`
  呼号/代码/日志。颜色一律 `var(--x, fallback)`。
- 布局：纵向流为主；viewBox 宽 ~960；箭头用 marker；`<figure>` + 中文图题。
- 页面 CSS 新增 `.doc-fig` 样式（卡片背景、边框、圆角、figcaption 居中灰字）。

## 构建与验证

- `build_new_dxcc_doc.py`：新增 `inline_svg_figures()`——正则匹配
  `<img src="../images/NAME.svg" alt="CAPTION" />`，替换为
  `<figure class="doc-fig">SVG 内容<figcaption>CAPTION</figcaption></figure>`；
  模板 `<style>` 追加 `.doc-fig` 样式；`octen.css?v=4→v5` 缓存戳（如有变更）。
- 验证：pandoc 重建成功；`xmllint --noout` 每个 SVG；输出 HTML 无残留 ASCII 图
  （grep 制表符/box-drawing）；13 个 `<figure class="doc-fig">`；图序 1–13 连续。
- 发布：`website/deploy.sh`（自带远端备份与 nginx reload）。

## 不做的事

- 不改其他页面（index/sdd/*）；不改 CSS 主题；不动服务端代码；不新增英文版。
