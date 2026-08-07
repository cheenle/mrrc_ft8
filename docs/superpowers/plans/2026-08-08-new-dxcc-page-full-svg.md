# new-dxcc-auto-call 页面全面更新 + 全 SVG 配图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `website/zh/new-dxcc-auto-call.html`（源 `docs/new-dxcc-auto-call.md`）全面更新至 SDD V1.5，把全部图示 SVG 化（13 张，构建时内联），并发布到 www.vlsc.net。

**Architecture:** Markdown 仍是单一事实源，pandoc 转 HTML；`build_new_dxcc_doc.py` 新增 SVG 内联步骤，把 `![图题](../images/x.svg)` 展开为 `<figure class="doc-fig">SVG+figcaption</figure>`，使 SVG 继承页面 CSS 变量与 webfonts。SVG 源文件放 `website/images/`。

**Tech Stack:** Python 3.11 构建脚本（pandoc 3.9）、手写 SVG 1.1（无外部依赖）、pytest（文本/契约回归）、bash deploy.sh。

**Spec:** `docs/superpowers/specs/2026-08-08-new-dxcc-page-full-svg-design.md`

## Global Constraints

- 主题色（全部带 fallback）：主流程/强调 `var(--accent,#22d3ee)`；节点填充 `var(--bg-secondary,#0d1117)`；画布 `var(--bg-tertiary,#10161e)`；主文本 `var(--text-primary,#ffffff)`；次文本 `var(--text-secondary,#8899aa)`；弱化 `var(--text-muted,#5c6370)`；故障/跳过/重试琥珀 `#f59e0b`；成功/落库绿 `#34d399`；错误/冲突红 `#f87171`。
- SVG 字体：正文 `font-family="Inter, system-ui, -apple-system, sans-serif"`；呼号/代码/日志 `font-family="'JetBrains Mono', ui-monospace, monospace"`。
- SVG 规范：每张根元素含 `xmlns`、`viewBox`、`role="img"`、`aria-labelledby` + `<title>`；画布矩形 `rx="12"` 铺满；所有 `defs`/marker/gradient id 以图 slug 前缀（如 `f1-arr`）保证内联后全局唯一；节点圆角 `rx="8"`、描边 `stroke-width="1.5"`；箭头线宽 1.5–1.75；字号标题 15–16、正文 12.5–13、注释 11–12。
- Markdown 图引用格式：`![图 N 图题：说明](../images/<file>.svg)`，pandoc 输出 `<img src="../images/<file>.svg" alt="图 N ..." />`，构建脚本据此内联。
- 技术事实（已对照代码核实，写文档/图时逐字使用）：
  - `_CTY_NAME_ALIASES`：`Germany → Fed. Rep. of Germany`、`Malaysia → West Malaysia`、`Turkey → Asiatic Turkey`；`_canonical_entity_name()` 未知名原样返回；实测 42 个 pskreporter 实体名仅这 3 个不一致。
  - `serial_guard()`（restart.sh）：`lsof -t "$RIG_DEVICE"` 找持有者，非 rigctld 持有即 `✗ CAT 串口被非 rigctld 进程占用 (PID …)` 并 `exit 1`；`MRRC_FT8_SKIP_SERIAL_GUARD=1` 跳过。
  - `pick_cq_frequency`：`DEFAULT_GUARD_HZ=30.0`、`DEFAULT_SEARCH_WINDOW_HZ=300.0`、`DEFAULT_OCCUPANCY_TTL_S=120.0`、默认回退 `DEFAULT_TX_AUDIO_FREQUENCY=1500.0`。
  - `TxDriver`：`TX_DECISION_CUTOFF_SECONDS=5.0`、`TX_WAVEFORM_SECONDS=12.64`、`TX_FIT_MARGIN_SECONDS=0.2`、15 s slot。
  - `Sequencer.max_retransmissions=3`（NFR-055：1 发 + 3 重传）；`DisarmReason.PARTNER_LOST / RETRY_EXHAUSTED`；应答状态序 `REPLYING → REPORT → ROGER_REPORT → ROGERS → SIGNOFF → DONE`。
  - `worked_dxcc = {e.name for e in state.dxcc_cache.entities}`；`on_decode` 中 `dxcc_cache is None or repository.dxcc_dirty` 时同步重建（`dxcc_summary`，~0.2 s，每 slot 至多一次）。
  - `_auto_call`：`safety.arm()` 抛 `TxRefused` → `auto_call skipped: interlock open (<call>)`；成功 → `auto_call: <call> snr=… slot=…` + audit（actor=system, operation=auto_call, detail=`snr=… new_dxcc`）。

---

### Task 1: 构建脚本 SVG 内联 + 回归测试（TDD）

**Files:**
- Modify: `website/build_new_dxcc_doc.py`
- Test: `tests/test_website_new_dxcc_doc.py`（Create）

**Interfaces:**
- Produces: `inline_svg_figures(html: str, out_path: Path) -> str` — 把 `<img src="../images/NAME.svg" alt="ALT" />` 替换为 `<figure class="doc-fig"><svg…>…</svg><figcaption>ALT</figcaption></figure>`；svg 文件按 `out_path.parent / src` 解析；文件不存在抛 `SystemExit`。`main()` 在写文件前调用它。模板 `<style>` 追加 `.doc-fig` 样式。

- [ ] **Step 1: 写失败测试** `tests/test_website_new_dxcc_doc.py`

```python
"""Contract tests for the new-DXCC doc build (pandoc wrapper + SVG inlining)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "build_new_dxcc_doc", ROOT / "website" / "build_new_dxcc_doc.py"
)
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


def test_inline_svg_replaces_img_with_figure(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "demo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>',
        encoding="utf-8",
    )
    out = tmp_path / "zh" / "page.html"
    html = '<p>x</p>\n<img src="../images/demo.svg" alt="图 1 演示" />\n<p>y</p>'
    result = build.inline_svg_figures(html, out)
    assert '<figure class="doc-fig">' in result
    assert "<figcaption>图 1 演示</figcaption>" in result
    assert 'viewBox="0 0 10 10"' in result
    assert "<img" not in result


def test_inline_svg_missing_file_fails(tmp_path: Path) -> None:
    out = tmp_path / "zh" / "page.html"
    html = '<img src="../images/nope.svg" alt="x" />'
    with pytest.raises(SystemExit):
        build.inline_svg_figures(html, out)


def test_non_svg_img_untouched(tmp_path: Path) -> None:
    out = tmp_path / "zh" / "page.html"
    html = '<img src="../images/cockpit-ft8.jpg" alt="cockpit" />'
    assert build.inline_svg_figures(html, out) == html
```

- [ ] **Step 2: 跑测试确认失败**：`venv/bin/python -m pytest tests/test_website_new_dxcc_doc.py -v` → FAIL（`inline_svg_figures` 不存在）。
- [ ] **Step 3: 实现**：`website/build_new_dxcc_doc.py` 增加：

```python
IMG_SVG_RE = re.compile(r'<img src="(\.\./images/[^"]+\.svg)" alt="([^"]*)"\s*/?>')

def inline_svg_figures(html: str, out_path: Path) -> str:
    def _sub(m: re.Match[str]) -> str:
        svg_path = (out_path.parent / m.group(1)).resolve()
        if not svg_path.is_file():
            raise SystemExit(f"missing svg figure: {svg_path}")
        svg = svg_path.read_text(encoding="utf-8").strip()
        caption = f"<figcaption>{m.group(2)}</figcaption>" if m.group(2) else ""
        return f'<figure class="doc-fig">{svg}{caption}</figure>'
    return IMG_SVG_RE.sub(_sub, html)
```

`main()` 改为 `body_html = inline_svg_figures(build_page(body), OUT)` 后写盘。模板 `<style>` 块追加：

```css
.doc-fig { margin: 1.5rem 0; padding: 1rem; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 0.75rem; }
.doc-fig svg { width: 100%; height: auto; display: block; }
.doc-fig figcaption { margin-top: 0.75rem; font-size: 0.8125rem; color: var(--text-muted); text-align: center; line-height: 1.5; }
```

- [ ] **Step 4: 跑测试确认通过**：同上命令 → 3 passed。
- [ ] **Step 5: Commit**：`git add website/build_new_dxcc_doc.py tests/test_website_new_dxcc_doc.py && git commit -m "feat(website): inline SVG figures in new-DXCC doc build"`

---

### Task 2: Markdown 全面更新至 SDD V1.5（含图引用占位）

**Files:**
- Modify: `docs/new-dxcc-auto-call.md`（整体重写）

**Interfaces:**
- Produces: 更新后的 MD，13 处 `![图 N …](../images/<file>.svg)` 引用（文件名与 Task 3–7 产物一致）；原两处 ASCII 图删除；§7 重组为 7.1/7.2/7.3。

内容变更清单（其余段落保持原文）：

1. 头部引用块 → `适用版本：SDD V1.5（2026-08-08，含 UC-003/UC-004 频率纪律、AD-008 串口守卫、band-hunt 实体名归一化）。`
2. §2 ASCII 图 → `![图 1 端到端链路总览：从空中解码到 worked 集刷新](../images/auto-call-pipeline.svg)`
3. §3.1 末尾 → `![图 2 信号采集与解码链：进程边界与采样率铁律](../images/capture-decode.svg)`
4. §3.2 判定段后 → `![图 3 DXCC 实体判定：cty.dat 三规则 lookup 与 worked 集比较](../images/dxcc-lookup.svg)`
5. §3.3 代码后 → `![图 4 决策门 auto_call_candidate：六条件 AND，每 slot 至多命中第一个](../images/decision-gate.svg)`
6. §3.4 末尾 → `![图 5 触发与安全门：safety.arm 互锁矩阵与 skip/重试](../images/arm-interlock.svg)`
7. §3.5 末尾 → `![图 6 应答发射 TX 链：TxDriver 时隙泵 → safety.transmit → rigctld](../images/tx-chain.svg)`
8. §3.6 ASCII 图 → `![图 7 QSO 状态机（应答侧）：REPLYING → DONE 与中断出口](../images/qso-states.svg)`
9. §3.7 末尾 → `![图 8 落库与 worked 集刷新：dxcc_dirty 闭环](../images/persist-worked.svg)`
10. §3.8：第一条 bullet 后插入新 bullet：

    > - **实体名归一化**（V1.5，2026-08-08）：pskreporter 用普通名（`Germany`/`Malaysia`/`Turkey`），cty.dat 规范名是（`Fed. Rep. of Germany`/`West Malaysia`/`Asiatic Turkey`）——`rank_bands` 直接比对会失配，已通联实体被误判 new（现场：反复切 15 m 追 Germany，而 qso 表已有 64 条 DL/DK）。`_CTY_NAME_ALIASES` + `_canonical_entity_name()` 归一化后用于 worked 判定；`new_entities` 保留原名显示；未知名称原样通过不误伤。auto-call 的 `is_new_dxcc` 不受影响（两侧都用 `cty.lookup`，本就一致）。

    §3.8 末尾 → `![图 9 band-hunt 闭环：双闸门 → 轮询 → 归一化 → 排序切频 → 自动呼叫](../images/band-hunt-loop.svg)`
11. §4 表新增行：`| MRRC_FT8_SKIP_SERIAL_GUARD | 环境变量 | 未设置（守卫生效） | 应急跳过 restart.sh 串口占用守卫（AD-008） |`
12. §6 不变量 7 → `**不碰 CAT 串口**：唯一 owner 是 rigctld（AD-008）。部署期由 restart.sh serial_guard() 强制：启动 rigctld 前 lsof 检测串口持有者，非 rigctld 进程持有即拒绝启动并列出持有者（fail-fast）；MRRC_FT8_SKIP_SERIAL_GUARD=1 应急跳过。` 节末 → `![图 10 安全不变量：单点 TX 门、STOP 优先、不自动恢复](../images/safety-gate.svg)`
13. §7 重组：
    - 标题保留；`**背景**` 段改为：`2026-08-07 现场连续暴露三个根因：TN8GD 自动呼叫的频率纪律、CAT 串口争抢、band-hunt 实体名失配。以下按时间复盘。`
    - 现有 TN8GD 内容收进 `### 7.1 TN8GD 频率纪律（UC-003/UC-004）`（表格+根因+修复原文保留），表格前 → `![图 11 TN8GD 频率根因：应答必须发在伙伴解码频率](../images/tn8gd-frequency.svg)`
    - 新增 `### 7.2 CAT 串口争抢与 serial_guard（AD-008，V1.4）`：18:01 一台手动 nohup 的旧 mrrc_ft710 `server.py`（MacPorts Python）直接 open `/dev/cu.usbserial-0121DB3A0`，与 rigctld 共持 → 字节争抢 → 18:00–22:20 rig 轮询 ~90% 超时（`wrong reply`/`Rig busy` 数千条），band_hunt 切频 5 次失败；停掉残留进程（PID 56041）立即恢复。修复：`restart.sh serial_guard()`（见 §6 不变量 7）；旧项目 `switch.sh/stop.sh` 同步加固（大小写不敏感 `pgrep -if`、串口持有者兜底）。图 → `![图 12 CAT 串口争抢与 serial_guard 守卫](../images/serial-guard.svg)`
    - 新增 `### 7.3 band-hunt 实体名失配与归一化（V1.5）`：12 h 复盘——band_hunt 反复切 15 m 追 "Germany"，但 Germany 早已通联（qso 表 64 条 DL/DK）；根因：worked 过滤直接拿 pskreporter 普通名对 cty 规范名失配（42 个实测实体名中仅 3 个不一致）；修复 `_CTY_NAME_ALIASES` + `_canonical_entity_name()`；auto-call 的 `is_new_dxcc` 不受影响。图 → `![图 13 实体名归一化：pskreporter → cty 规范名](../images/entity-normalize.svg)`
14. §8 新增 bullet：`- **实体名别名表**：`_CTY_NAME_ALIASES` 目前只覆盖 3 个已知失配（Germany/Malaysia/Turkey）；pskreporter 若引入新的不一致名称需人工扩充别名表。`

- [ ] **Step 1: 重写 MD**（按上述清单）
- [ ] **Step 2: pandoc 干跑验证语法**：`pandoc docs/new-dxcc-auto-call.md -f markdown -t html --no-highlight --wrap=none -o /tmp/ndc-check.html && grep -c '<img src="../images/' /tmp/ndc-check.html` → 输出 `13`；`grep -c '┌\|└\|▼' /tmp/ndc-check.html` → `0`（ASCII 图已清）
- [ ] **Step 3: Commit**：`git add docs/new-dxcc-auto-call.md && git commit -m "docs(new-dxcc): full update to SDD V1.5 + SVG figure references"`

---

### Task 3: 图 1（端到端流水线）与图 7（QSO 状态机）

**Files:**
- Create: `website/images/auto-call-pipeline.svg`（viewBox 960×1250）
- Create: `website/images/qso-states.svg`（viewBox 960×680）

**图 1 契约**（纵向，居中主链，节点宽 700）：
- 顶部宽盒（青框）：`FT-710 → capture_proc 子进程 → UtcRing(绝对序号) → DSP Worker`，上沿标签 `12 kHz int16 单声道（铁律）`，右侧注释 `每 15 s slot 一批解码`。
- 盒 `orchestrator.on_decode（main.py）`，其下并排两小盒：左 `1. DXCC cache 刷新 — dxcc_dirty 时同步重建，~0.2 s，每 slot 至多一次`；右 `2. decode_message_view — is_new_dxcc = lookup(call) ∈ 实体 ∧ ∉ worked`。
- 顺序下接（每盒带序号圆标）：
  3 `auto_call_candidate 决策（纯函数）` 副行 mono：`开关开 ∧ 空闲 ∧ 无选择 ∧ is_new_dxcc ∧ is_cq ∧ ¬mine`；
  4 `_auto_call：safety.arm()（互锁门）→ sequencer.reply_to()` 副行：`tx_phase = 1 − slot%2 · tx_frequency = 伙伴解码频率（UC-003）` 与琥珀小字 `失败 → 记日志跳过，下一 slot 重试 · audit 落 auto_call`；
  5 `TX 链路：TxDriver（奇偶泵 + I9 窗口 + fit guard）` 副行：`next_tx_message() → dsp_encode 48 kHz → safety.transmit（PTT 门 + watchdog + 预算）`；
  6 `sequencer QSO 状态机（NFR-055：1 发 + 3 重传）` 副行 mono：`REPLYING → REPORT → ROGER_REPORT → ROGERS → SIGNOFF/DONE`；
  7 `落库：_ensure_log → on_qso → qso_log.enqueue → record_qso` 副行：`qso 表 source='live' · dxcc_dirty = True`（绿框）；
  8 `下一 slot：DXCC cache 重建 → 实体进 worked → 不再触发`。
- 盒 8 左侧绿色虚线回环箭头指向盒 3 区域，标签 `worked 集闭环`。

**图 7 契约**（状态梯，纵向）：
- 顶部灰色注记：`应答侧（我们是应答者）· 每条消息 1 发 + 至多 3 重传（NFR-055）`。
- 状态盒（青框，盒内上行 mono 状态名、下行消息）：
  `REPLYING` Tx1 `<dx> <my> <my-grid>`（先发本台呼号+网格）；
  `REPORT` Tx2 `<dx> <my> -snr`；
  `ROGER_REPORT` Tx3 `<dx> <my> R-snr`；
  `ROGERS` Tx4 `<dx> <my> RR73`；
  `SIGNOFF → DONE` Tx5 `<dx> <my> 73`（绿框，`→ 落库`）。
- 状态间箭头标签（伙伴触发）：`对方报告到达`、`对方 R 报告到达`、`对方 RR73/73 到达`。
- 右侧两个琥珀出口盒：`PARTNER_LOST — 伙伴转呼他人 → anti-QRM 自动停`、`RETRY_EXHAUSTED — 预算耗尽 → 停并保留伙伴上下文`；两者汇入灰色盒 `on_stop：清除 selected（P7）`。
- 左下注释：`on_message 只处理 addressed-to-me；自身回波（mine）忽略`。

**Steps（每张图相同流程，图 1 → 图 7 顺序执行）：**
- [ ] **Step 1: 按契约写 SVG**（Global Constraints 的色板/字体/id 前缀 `f1-`/`f7-`）
- [ ] **Step 2: XML 校验**：`xmllint --noout website/images/auto-call-pipeline.svg website/images/qso-states.svg` → 无输出
- [ ] **Step 3: 标签齐全回归**：`grep -c 'auto_call_candidate\|safety.arm\|worked' website/images/auto-call-pipeline.svg` ≥ 3；`grep -c 'REPLYING\|ROGER_REPORT\|PARTNER_LOST\|RETRY_EXHAUSTED' website/images/qso-states.svg` ≥ 4
- [ ] **Step 4: Commit**：`git add website/images/auto-call-pipeline.svg website/images/qso-states.svg && git commit -m "feat(website): SVG figures — end-to-end pipeline + QSO state machine"`

---

### Task 4: 图 2（采集解码链）、图 3（DXCC lookup）、图 4（决策门）

**Files:**
- Create: `website/images/capture-decode.svg`（960×520，横向链）
- Create: `website/images/dxcc-lookup.svg`（960×560）
- Create: `website/images/decision-gate.svg`（960×500）

**图 2 契约**：横向节点：`FT-710`（rig）→ `capture_proc 子进程`（注：进程边界隔离 CoreAudio 会话退化）→ `UtcRing — 绝对序号 slot_id % capacity` → `DSP Worker — 全局锁 · profiles 0–4 · A8 门控` → `DecodeResult`（mono：slot_id/snr/dt/frequency/text）→ `orchestrator.on_decode`。子进程边界用竖向虚线框住 capture_proc。底部两条标注：青 `RX 恒 12 kHz int16 单声道（binding.py 强制）`、青 `TX 波形恒 48 kHz`。

**图 3 契约**：左列 `cty.dat（country-files ADIF）` 文件盒 → `CtyDatabase` 盒，内三行规则 chip：`=CALL 精确前缀优先`、`(数字) 替换加载时展开`、`最长前缀匹配（TN → Niger）`；下挂小盒 `索引化：精确表 + 前缀 trie · 单次亚微秒`。右列流程：mono `lookup("TN8GD")` → `实体 = Niger` → 菱形判定 `∈ worked 集？`（worked 集来源注：`dxcc_summary — qso 非 void × cty 查表`）→ 否 → 绿 `is_new_dxcc = true`；是 → 灰 `不触发`。右下琥珀小注：`cache 未建 → 保守 False（不误触发）`。

**图 4 契约**：六个条件 chip 纵向排列（每条前 mono 序号 1–6）：`auto_call_enabled — setting_meta.auto_call_new_dxcc`、`¬ has_selection — QSO 结束清 selected（P7）`、`sequencer_state == IDLE — 无 QSO、无 CQ 循环`、`is_new_dxcc`、`is_cq`、`¬ mine — 自身回波不触发`；六条线汇入右侧 AND 门形（圆角盒 `全部满足`，青框）→ `命中本 slot 第一个候选 → _auto_call`。顶部注：`每 slot 按解码顺序检查，至多命中第一个`。

**Steps:**
- [ ] **Step 1: 按契约写 3 张 SVG**（id 前缀 `f2-`/`f3-`/`f4-`）
- [ ] **Step 2: XML 校验**：`xmllint --noout website/images/capture-decode.svg website/images/dxcc-lookup.svg website/images/decision-gate.svg`
- [ ] **Step 3: 标签回归**：`grep -c 'UtcRing\|capture_proc\|12 kHz' website/images/capture-decode.svg` ≥ 3；`grep -c 'trie\|worked\|Niger' website/images/dxcc-lookup.svg` ≥ 3；`grep -c 'IDLE\|is_cq\|mine' website/images/decision-gate.svg` ≥ 3
- [ ] **Step 4: Commit**：`git add website/images/capture-decode.svg website/images/dxcc-lookup.svg website/images/decision-gate.svg && git commit -m "feat(website): SVG figures — capture chain, DXCC lookup, decision gate"`

---

### Task 5: 图 5（arm 互锁门）、图 6（TX 链）

**Files:**
- Create: `website/images/arm-interlock.svg`（960×560）
- Create: `website/images/tx-chain.svg`（960×540）

**图 5 契约**：左上 `候选命中（图 4）→ _auto_call`；中心 `safety.arm()` 门盒；上方四枚互锁 chip：`CAT`、`audio`、`DSP`、`clock`（任一 faulted → 拒绝）。左下灰注：`系统级、无人值守：不需要控制租约（NFR-087）`。两条分支：琥珀 `任一互锁 faulted → TxRefused → log: auto_call skipped: interlock open (<call>) → 跳过本 slot，下一 slot 重试（不崩溃、不风暴）`；绿色 `arm 成功 → 记录选中消息（call/grid/snr/slot）→ sequencer.reply_to(tx_phase = 1 − slot%2, tx_frequency = 伙伴解码频率)` + 绿小盒 `audit_event：actor=system · operation=auto_call · detail=snr=… new_dxcc`。

**图 6 契约**：横向链：`TxDriver 时隙奇偶泵`（注：只在 tx_phase 对应奇偶 slot 拉取至多一条）→ `I9 决策窗口 5.0 s`（注：空闲持续轮询，一 arm 就发）→ `fit guard`（mono：`12.64 s + 0.2 s ≤ 15 s slot`，琥珀虚线分支 `超时 → deferring 下一 slot`）→ `dsp_encode(message, tx_frequency, slot_id) — 48 kHz`（注：应答=伙伴频率 split；CQ=占用环空闲频点）→ `safety.transmit()`（注：faults/armed/PTT/聚合预算复核 + watchdog，STOP 可随时取消）→ `rigctld（串口唯一 owner）` → `FT-710 PTT`。

**Steps:**
- [ ] **Step 1: 按契约写 2 张 SVG**（id 前缀 `f5-`/`f6-`）
- [ ] **Step 2: XML 校验**：`xmllint --noout website/images/arm-interlock.svg website/images/tx-chain.svg`
- [ ] **Step 3: 标签回归**：`grep -c 'TxRefused\|interlock\|audit' website/images/arm-interlock.svg` ≥ 3；`grep -c 'fit guard\|12.64\|rigctld' website/images/tx-chain.svg` ≥ 3
- [ ] **Step 4: Commit**：`git add website/images/arm-interlock.svg website/images/tx-chain.svg && git commit -m "feat(website): SVG figures — arm interlock gate + TX chain"`

---

### Task 6: 图 8（落库闭环）、图 9（band-hunt 闭环）

**Files:**
- Create: `website/images/persist-worked.svg`（960×560）
- Create: `website/images/band-hunt-loop.svg`（960×640）

**图 8 契约**：纵向主链（绿调）：`收到 RR73/73` → `_ensure_log（恰好一次）` → `on_qso` → `qso_log.enqueue`（注：asyncio.to_thread）→ `record_qso`（副行：`qso 表 source='live' · status='completed' · my/dx call+grid · 双方 report · freq_hz/band` + `qso_event` + `dxcc_dirty = True`）。左侧琥珀分支：`进程崩溃 → data/qso-pending.jsonl 待办恢复（不丢记录）`（从 enqueue 引出）。底部回环：`下一 slot on_decode 查 dxcc_dirty → 重建 dxcc_summary（~0.2 s，每 slot 至多一次）→ 实体 ∈ worked → 不再触发`，绿色虚线回指顶部，标注 `P1 修复：cache 刷新独立于 band-hunt 开关`。

**图 9 契约**：环形布局 6 节点顺时针：`① 双闸门：设置 auto_band_hunt ∧ 冷却 1200 s` → `② 轮询 /api/band_hunt（pskreporter · HTTP 唯一跨库边界）` → `③ 实体名归一化 _CTY_NAME_ALIASES（V1.5）` → `④ rank_bands / decide_switch — 按未通联实体数降序` → `⑤ 空闲时 rig 调谐切频` → `⑥ 新波段解码 → auto-call 闭环` → 回 ①。中心灰字 `把波段切到新 DXCC 最密集的地方`。环外右下小注：`CQ 空闲频点（UC-004）：占用环 120 s TTL · 1500±300 Hz 螺旋 · guard 30 Hz · 全占用回退 1500`。

**Steps:**
- [ ] **Step 1: 按契约写 2 张 SVG**（id 前缀 `f8-`/`f9-`）
- [ ] **Step 2: XML 校验**：`xmllint --noout website/images/persist-worked.svg website/images/band-hunt-loop.svg`
- [ ] **Step 3: 标签回归**：`grep -c 'dxcc_dirty\|qso-pending\|worked' website/images/persist-worked.svg` ≥ 3；`grep -c 'rank_bands\|冷却\|归一化' website/images/band-hunt-loop.svg` ≥ 3
- [ ] **Step 4: Commit**：`git add website/images/persist-worked.svg website/images/band-hunt-loop.svg && git commit -m "feat(website): SVG figures — persist/worked loop + band-hunt loop"`

---

### Task 7: 图 10（安全门）、图 11（TN8GD 频率）、图 12（串口守卫）、图 13（实体归一化）

**Files:**
- Create: `website/images/safety-gate.svg`（960×540）
- Create: `website/images/tn8gd-frequency.svg`（960×480）
- Create: `website/images/serial-guard.svg`（960×500）
- Create: `website/images/entity-normalize.svg`（960×440）

**图 10 契约**：顶部三个来源盒：`Web UI / 人工操作`、`auto-call（system）`、`CQ 循环`，三箭头汇入中心红色调门盒 `SafetyController — 唯一 TX 门（AD-007）`（内两行：`arm()：互锁 CAT/audio/DSP/clock`、`transmit()：faults/armed/PTT/聚合预算 + watchdog`）→ `PTT` → `rigctld → FT-710`。右侧红盒 `STOP — 任何已认证会话（无需租约）· 幂等 · PTT-off 非阻塞`，红箭头直指 PTT 门。底部琥珀注：`fault 清除后仍需人工 re-arm —— no-recovery-auto-resumes-TX`。

**图 11 契约**：上下两条泳道共用 0–3000 Hz 频率轴（刻度 0/500/843/1500/2000/2500/3000，843 与 1500 高亮）。泳道 A（红调，标 `修复前（2026-08-07 现场）`）：`TN8GD CQ` 信号峰 @843（青）；`本台应答` 峰 @1500（红，标 `✗ 未配对 — 重传 4 次无应答`）；灰虚峰 @1500 标 `自身回波`。泳道 B（绿调，标 `UC-003 修复后`）：CQ @843；应答 @843（绿，标 `✓ split 配对`）。右侧小结：`应答必须发在伙伴解码频率`。

**图 12 契约**：上半（问题区，红调）：串口设备盒 `/dev/cu.usbserial-0121DB3A0（FT-710 CAT）` 被两条线连着两个持有者：`rigctld（合法 owner，青）` 与 `残留 mrrc_ft710 server.py（PID 56041，红）`；中间 `字节争抢` 闪电标注；后果行 mono：`rig 轮询 ~90% 超时（4 h）· wrong reply / Rig busy 数千条 · band_hunt 切频 5 次失败`。下半（修复区，绿调）：`restart.sh serial_guard()` 门盒（mono：`lsof -t "$RIG_DEVICE"`）→ `非 rigctld 持有 → fail-fast，列出持有者`；琥珀小门 `MRRC_FT8_SKIP_SERIAL_GUARD=1 应急跳过`。

**图 13 契约**：三列映射。左列（pskreporter 普通名，灰）：`Germany`、`Malaysia`、`Turkey`；中列 `_CTY_NAME_ALIASES` 盒（`_canonical_entity_name()`）；右列（cty 规范名，青）：`Fed. Rep. of Germany`、`West Malaysia`、`Asiatic Turkey`。右列后接 `worked 判定 ✓`（绿）注：`qso 表 64 条 DL/DK → Germany 不再判 new`。底部两条注：`42 个实测实体名仅 3 个不一致`；`未知名称原样通过（不误伤）· auto-call is_new_dxcc 不受影响`。

**Steps:**
- [ ] **Step 1: 按契约写 4 张 SVG**（id 前缀 `f10-`/`f11-`/`f12-`/`f13-`）
- [ ] **Step 2: XML 校验**：`xmllint --noout website/images/safety-gate.svg website/images/tn8gd-frequency.svg website/images/serial-guard.svg website/images/entity-normalize.svg`
- [ ] **Step 3: 标签回归**：每图至少命中契约关键词 3 个（如 `grep -c '843\|1500\|配对' website/images/tn8gd-frequency.svg` ≥ 3）
- [ ] **Step 4: Commit**：`git add website/images/safety-gate.svg website/images/tn8gd-frequency.svg website/images/serial-guard.svg website/images/entity-normalize.svg && git commit -m "feat(website): SVG figures — safety gate, TN8GD freq, serial guard, entity normalization"`

---

### Task 8: 重建 HTML + 全量验证

**Files:**
- Regenerate: `website/zh/new-dxcc-auto-call.html`

- [ ] **Step 1: 全量测试**：`venv/bin/python -m pytest tests/test_website_new_dxcc_doc.py tests/test_deploy_artifacts.py -v` → 全绿
- [ ] **Step 2: 重建**：`venv/bin/python website/build_new_dxcc_doc.py` → 打印生成字节数
- [ ] **Step 3: 结构验证**：
  - `grep -c '<figure class="doc-fig">' website/zh/new-dxcc-auto-call.html` → `13`
  - `grep -c '<figcaption>图' website/zh/new-dxcc-auto-call.html` → `13`
  - `grep -c '<img' website/zh/new-dxcc-auto-call.html` → `0`（无残留外链图）
  - `grep -c '┌\|└\|▼\|──' website/zh/new-dxcc-auto-call.html` → `0`（ASCII 图清零）
  - `grep -o 'SDD V1.5（2026-08-08' website/zh/new-dxcc-auto-call.html` 命中；`grep -c '_CTY_NAME_ALIASES\|serial_guard\|MRRC_FT8_SKIP_SERIAL_GUARD' website/zh/new-dxcc-auto-call.html` ≥ 6
- [ ] **Step 4: Commit**：`git add website/zh/new-dxcc-auto-call.html website/images/*.svg && git commit -m "docs(new-dxcc): regenerate website page for V1.5 (MD + HTML, published)"`（沿用 42ca97f 的 message 风格；若 Task 2–7 已分别 commit 图片则此处只 add html）

### Task 9: 部署到 www.vlsc.net

- [ ] **Step 1: 部署**：`cd /Users/cheenle/HAM/ft8/website && echo y | bash deploy.sh`（脚本先备份远端再上传、reload nginx；观察 `nginx -t` 与 `deployment complete`）
- [ ] **Step 2: 线上验证**：`curl -s https://www.vlsc.net/mrrc_ft8/zh/new-dxcc-auto-call.html | grep -c '<figure class="doc-fig">'` → `13`；`curl -s … | grep -c 'SDD V1.5'` ≥ 1
- [ ] **Step 3: 若有残留问题修复后重复 Step 1–2；最终确认无需再 commit 则结束**

---

## Self-Review 结论

- Spec 覆盖：内容更新 1–14（Task 2）、13 图（Task 3–7）、构建内联+CSS（Task 1）、验证（Task 8）、部署（spec 发布节 → Task 9）、不做的事（无对应任务，符合预期）。✓
- 无占位符；各图契约给出逐字标签。✓
- 一致性：文件名在 Task 2 引用与 Task 3–7 产物间一一对应；`inline_svg_figures(html, out_path)` 签名在测试与实现一致。✓
