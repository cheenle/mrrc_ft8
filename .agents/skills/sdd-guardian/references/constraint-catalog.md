# Constraint Catalog — MRRC-FT8 SDD V1.0

`harness/constraints.json` is the machine-readable source of truth.

## Block

| ID | Rule | Source |
|---|---|---|
| `vendor-readonly` | Never edit `wsjtx-3.0.2/` | AD-002, NFR-080 |
| `no-direct-ctypes` | Library loading only in Worker `binding.py` | AD-003/005, NFR-081 |
| `decoder-forbidden-rates` | Decode ABI is 12 kHz int16 mono | AD-004, NFR-007 |
| `no-direct-serial` | rigctld alone owns serial | AD-008 |
| `ptt-authority` | PTT only through rig/safety | AD-007, chapter 15 |
| `no-url-token` | No credentials in HTTP/WS URLs | AD-009, NFR-034 |
| `secrets-hardcoded` | No literal secrets | NFR-032 |
| `index-no-inline-js` | No inline app logic in HTML shell | NFR-082 |

## Warn / Guidance

- Device paths are configuration.
- Slot identity uses UTC epoch floor; late actions are skipped.
- DSP Worker uses the binding lock and Fortran-batched OpenMP results.
- Waterfall may drop old frames; safety/state/heartbeat/STOP may not.
- Every behavior change updates affected SDD chapters and chapter 14.

## Open Parameters

I8–I11 cover the exact IPC representation, measured thread/deadline policy, real-radio lead/lag and post-V1 Linux matrix. They do not reopen ownership, rate, lease or safety decisions.

