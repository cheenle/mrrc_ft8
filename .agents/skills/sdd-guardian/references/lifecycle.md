# Lifecycle Checklist — MRRC-FT8

## Context and Design

- [ ] Run `brief` for files and task; read referenced SDD chapters.
- [ ] Identify affected SC/NFR/AD/UC and R1–R10/A1–A6/I8–I11.
- [ ] Review every TX/PTT change against chapter 15.
- [ ] Preserve worker, audio-rate, UTC, rigctld and lease ownership invariants.

## Implementation and Test

- [ ] No block-level constraint violation.
- [ ] Hardware I/O uses asyncio thread offload where required.
- [ ] New behavior has hardware-free pytest coverage at the boundary.
- [ ] Fault paths disarm and never auto-resume TX.
- [ ] `venv/bin/python -m pytest tests/` passes.

## Verification and Documentation

- [ ] `sdd_context.py check <changed paths>` or `--staged` is clean.
- [ ] Affected SDD chapters and `SDD/14-version-history.md` are updated.
- [ ] AGENTS.md and test inventory remain accurate.
- [ ] Git mutation occurs only when the user requested it.

