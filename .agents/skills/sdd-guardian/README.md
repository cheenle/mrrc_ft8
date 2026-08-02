# SDD-Guardian — MRRC-FT8

`SDD/` 是 MRRC-FT8 的 15 章 TeamSD 设计记录；本 skill 将其变成可查询、可检查的工程护栏。

## Commands

```bash
H=.agents/skills/sdd-guardian/harness/sdd_context.py
python3 $H prime
python3 $H brief <paths>
python3 $H brief --task "任务描述"
python3 $H sdd AD-007
python3 $H trace docs/superpowers/specs/<spec>.md
python3 $H check <paths>
python3 $H check --staged
```

## Block Rules

- `wsjtx-3.0.2/` 只读。
- DSP 共享库只由 Worker 内 `server/core/binding.py` 加载。
- 解码 ABI 只收 12 kHz int16 单声道；TX 为 48 kHz。
- 应用不打开 CAT 串口，唯一 owner 是 rigctld。
- PTT 只允许 rig/safety 边界调用。
- Cookie 认证，禁止 URL token。
- `index.html` 无内联应用逻辑；禁止硬编码秘密。

常规流程：`brief` → 设计/实现 → `pytest` → `check` → 同步 SDD/14 → 经用户授权后提交。

