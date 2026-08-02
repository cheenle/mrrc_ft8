---
name: sdd-guardian
description: SDD-driven engineering lifecycle for MRRC-FT8 — WSJT-X Improved DSP worker, UTC/audio discipline, rigctld ownership, public Web control lease, PTT safety, testing and documentation sync
type: prompt
whenToUse: When creating, modifying, reviewing, or debugging code in this repository; when planning features or refactors; when the change touches CAT commands, audio, polling, PTT, state, WebSocket protocol, frontend UI, deployment, or documentation
arguments:
  - task
---

# SDD Guardian — engineering lifecycle for MRRC-FT8

This repository is governed by `SDD/` (IBM TeamSD, 15 chapters, currently **V1.0**)
— requirements, system context, architecture decisions, service/component models,
feasibility analysis, and version history. The SDD is the canonical design record:
your job on every change is to keep the runtime AND the design record consistent.
This skill is the enforcement loop; `${KIMI_SKILL_DIR}/harness/` is the machine-readable
backing: `constraints.json` (enforcement rules), `index.json` (knowledge routing into
every SDD chapter — resolved live, never a stale copy), and `sdd_context.py` (CLI).
${ARGUMENTS:+Task focus: $ARGUMENTS}

## Phase 0 — Load the full engineering brief (always, before touching code)

```bash
python3 ${KIMI_SKILL_DIR}/harness/sdd_context.py brief <files-you-will-touch>
python3 ${KIMI_SKILL_DIR}/harness/sdd_context.py brief --task "<one-line task description>"
```

`brief` extracts, live from the SDD, everything relevant to those files/topics:

- **Architecture decisions** (AD-001…AD-015) with problem/decision/consequences
- **Requirements** (NFR-001…084 with targets + verification; success criteria SC1–SC10)
- **Use cases** (UC-001…010) your change must keep working
- **Feasibility**: risks R1–R10 + mitigations, assumptions A1–A6, open issues I8–I11
- **Constraints** (block/warn/info rules for those files)

Need one specific item later? `sdd AD-011` · `sdd NFR-060` · `sdd UC-005` ·
`sdd R4` · `sdd I8` · `sdd 9.6` · `sdd <keyword>`. For anything beyond a trivial
fix, also read the referenced SDD chapter in full.

## Phase 1 — Design check (requirements → decision → feasibility)

- **Requirements traceability**: which SC/NFR does this change serve or affect?
  If it could degrade an NFR target (latency, bandwidth, safety), that is a
  design conversation with the user — not a unilateral code edit.
- **Architecture decisions**: which ADs does this change touch? Contradicting an
  AD means amending `SDD/08` in the same change — never silently diverging.
- **Feasibility**: does the change rely on a chapter 13 risk or assumption
  (R1–R10, A1–A6)? Does it assume open implementation parameters I8–I11 are
  already measured? Do not weaken an invariant to close an open parameter.
- **Use cases**: walk the affected UC main flow + exceptions end-to-end mentally.
- **Safety**: anything touching PTT/TX — Chapter 15 is load-bearing. STOP is
  priority/idempotent; PTT release is non-blocking; fault/restart never resumes TX.

## Phase 2 — Implement under constraint

Golden rules (block-level; the PreToolUse hook rejects these edits):

- Never modify `wsjtx-3.0.2/`; register required copies under `dsp/patched/`.
- Decoder input is exactly 12 kHz mono int16; TX waveform is exactly 48 kHz.
- Shared-library loading and ABI calls live only in `server/core/binding.py` and
  execute only inside the supervised DSP Worker.
- Every DSP call takes the binding global lock; OpenMP threads batch results in
  Fortran and never callback into Python.
- `rigctld` is the serial owner; application code never opens CAT serial.
- PTT calls exist only at the rig/safety boundary; Web/DSP/sequencer cannot key directly.
- Authentication uses Secure HttpOnly cookies — never URL query tokens.
- No inline application JS in `server/web/static/index.html`; no hardcoded secrets.

Guidance: slot identity is `floor(epoch/TRperiod)`; late actions are skipped;
waterfall may drop old frames but state/lease/STOP may not; failure recovery is
monitor-only and always requires manual lease acquisition/re-arm.

Minimal diffs. Match the module's existing conventions (AGENTS.md style section).

## Phase 3 — Test

```bash
venv/bin/python -m pytest tests/
```

- Every bug fix gets a regression test; every feature gets coverage if the
  logic is hardware-independent (mock at the serial/audio boundary).
- Sync `tests/README.md` coverage inventory when tests change.

## Phase 4 — Verify against the harness

```bash
python3 ${KIMI_SKILL_DIR}/harness/sdd_context.py check --staged
```

Must print `clean` (or warnings you can justify) before committing.

## Phase 5 — Documentation sync (part of the change, not an afterthought)

| If you changed… | Then also update |
|---|---|
| DSP ABI/worker/Improved profile | SDD §9.1/§9.2, AD-002/003/005, chapter 11 |
| Audio rates/timing/waterfall | SDD §9.2/§9.4, AD-004/006/013 |
| Auth/lease/WS protocol | SDD chapter 10, AD-009/010/013 |
| PTT/sequencer behavior | SDD chapter 15, AD-007/012 |
| Persistence/retention | SDD §12.7, AD-014 |
| Architecture approach | SDD/08 (new or amended AD) |
| Module responsibilities | AGENTS.md module table |
| Test inventory | tests/README.md |
| ANY behavior change | SDD/14-version-history.md new entry + SDD/README Quick Facts version bump |

If the SDD contradicts the runtime you just verified, the SDD is wrong — fix it
in the same commit and say so in the version-history entry.

## Phase 6 — Commit

Short imperative summary, scoped commits (one logical change per commit).
Git mutations only when the user asks. Hardware-dependent changes note the
test environment (radio model, serial port, FT4222, audio device).

## Superpowers integration (two layers, one lifecycle)

The superpowers set (`~/.codex/skills`, wired via `extra_skill_dirs`) drives
**process** discipline; this skill drives **content** discipline. They compose —
never duplicate: full contract in `${KIMI_SKILL_DIR}/references/workflow-integration.md`.

| Phase | Superpowers skill | SDD-Guardian action |
|---|---|---|
| 创意/需求 | `brainstorming` | `brief --task`; spec must cite SDD refs → verify with `trace` |
| 计划 | `writing-plans` / `create-plan` | `brief <files>`; plan includes affected chapters + doc-sync task |
| 执行 | `executing-plans` / `subagent-driven-development` / `test-driven-development` | hook auto-blocks violations; paste `context` output into subagent briefs |
| 调试 | `systematic-debugging` | read `references/constraint-catalog.md` FIRST — it is this project's incident history |
| 验证 | `verification-before-completion` | gate = pytest green + `check --staged` clean + Phase 5 done |
| 评审 | `requesting-code-review` / `receiving-code-review` | attach `brief` output; constraints.json as review checklist |
| 收尾 | `finishing-a-development-branch` | Phase 5 doc-sync + SDD/14 version entry before merge/PR |

Priority on conflict (per superpowers' own rule that user instructions outrank
skills): `SDD/`, `AGENTS.md`, and `constraints.json` are project law — hook
blocks and SDD constraints always win over any workflow-skill default.

## Harness reference

```
sdd_context.py prime                    # session-start digest (SessionStart hook)
sdd_context.py brief <paths> [--task]   # FULL engineering brief: constraints +
                                        #   live-extracted ADs/NFRs/UCs/risks/issues
sdd_context.py context <paths> [--task] # fast view: SDD refs + constraints only
sdd_context.py sdd <AD-011|NFR-060|UC-005|R4|I8|SC8|9.6|keyword>  # one item
sdd_context.py trace <spec-or-plan.md>  # spec/plan ↔ SDD citation audit (advisory)
sdd_context.py check <paths>|--staged   # pattern scan; exit 2 on block violations
sdd_context.py hook                     # PreToolUse mode (stdin JSON), exit 2 blocks
```

Knowledge architecture: `constraints.json` holds enforcement rules;
`index.json` routes files/topics to typed SDD refs but stores **no content** —
`brief`/`sdd` slice the live `SDD/*.md` files at query time, so the harness can
never drift stale from the design record. When you add a new engineering area,
extend `index.json` topics; when the SDD gains sections, existing refs resolve
to the new text automatically.

Optional automatic enforcement (recommended): install the hooks from
`${KIMI_SKILL_DIR}/harness/hooks.snippet.toml` into `~/.kimi-code/config.toml`
(`python3 ${KIMI_SKILL_DIR}/harness/install_hooks.py` does it idempotently).
That injects `prime` at every session start and runs `hook` before every
Edit/Write so block-level violations are rejected automatically.

Deep reference: `${KIMI_SKILL_DIR}/references/constraint-catalog.md` (full rule
catalog with rationale) and `${KIMI_SKILL_DIR}/references/lifecycle.md`
(phase checklists). Source of truth when they disagree: `SDD/` itself.
