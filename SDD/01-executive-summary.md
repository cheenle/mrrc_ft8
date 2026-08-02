# 1. Executive Summary

## 1.1 Project Overview

MRRC-FT8 是基于 WSJT-X 3.0.2 Improved DSP 的 headless FT8/FT4 服务器。后台主进程拥有认证、控制租约、UTC 编排、音频、rigctld、PTT watchdog、QSO sequencer 与持久化；独立 DSP Worker 独占 Fortran/OpenMP 状态；移动浏览器通过 Caddy TLS 远程操作。

首版先交付普通 FT8 QSO：操作者人工选择 CQ 或候选台，sequencer 只自动完成这一场 QSO，不连续自动挑台。FT4 在 FT8 双平台验收后加入。

## 1.2 Architecture Summary

```text
Landscape Mobile PWA --HTTPS/WS--> Caddy :443
                                      |
                                      v loopback
                                FastAPI main
                    auth / lease / UTC / audio / rig / PTT
                                      |
                                      v supervised IPC
                                DSP Worker
                    binding lock / packjt77 / ft8var OpenMP
                                      |
                                      v
                              wsjt_core shared lib

FastAPI --loopback TCP--> rigctld --serial--> Radio
FastAPI --48 kHz PortAudio------------------> Radio USB audio
```

## 1.3 Load-bearing Rules

- Decoder input is always 12 kHz mono int16; TX waveform is always 48 kHz.
- Every DSP call is made by the worker through `server/core/binding.py` and its global lock.
- OpenMP threads aggregate Fortran results; they never call Python.
- `floor(epoch/TRperiod)` is the only slot identity rule.
- `rigctld` is the only serial owner.
- Every TX passes through sequencer and the central safety controller.
- Any authenticated session can issue immediate `STOP TX`; starting TX requires the single control lease.
- Restart or fault restores monitoring only, never PTT, armed TX or a lease.
- `wsjtx-3.0.2/` is read-only.

## 1.4 Product Shape

| Area | First release |
|---|---|
| DSP | FT8 standard + Improved `ft8var`, OpenMP Auto/1–12; FT4 follows |
| Operation | Human selects station; one standard QSO is sequenced |
| UI | Full landscape cockpit; degraded portrait observer/STOP view |
| Public access | Domain + Caddy automatic TLS + strong password |
| Concurrency | Many observer sessions, one 5s-heartbeat/15s-TTL control lease |
| Persistence | SQLite canonical QSO/audit/history + ADIF export |
| Deployment | macOS LaunchAgent and Linux systemd; no first-release Docker |
| Telemetry | Local only |

## 1.5 Delivery State

Version V1.0 is the approved design baseline. Existing Python files are exploratory skeletons, not a completed runtime. Milestones proceed as a FT8 vertical slice: DSP regression, engine/safety, API/Web, macOS real hardware, Linux simulation, then FT4.

