# 5. Non-Functional Requirements (ART 0507)

## 5.1 Performance and Timing

| ID | Requirement | Target / Verification |
|---|---|---|
| NFR-001 | Safe FT8 decode deadline | Profile 3 result available before its TX decision cutoff; late result cannot trigger late TX |
| NFR-002 | Decode latency visibility | Per-profile/thread latency histogram and deadline-miss counter |
| NFR-003 | Decode capacity | Typical busy-band batch is returned without truncation; overflow is explicit |
| NFR-004 | Waterfall cadence | Target ~3.5 lines/s; old frames drop under backpressure |
| NFR-005 | State latency | Safety/state event reaches a healthy client within 500 ms on LAN |
| NFR-006 | Slot identity | Always `floor(epoch/TRperiod)`; epoch-injected tests |
| NFR-007 | Audio domains | Decoder 12 kHz int16 mono; TX 48 kHz mono; binding asserts |
| NFR-008 | Clock safety | Clock step/unsafe offset inhibits TX until stable and manually re-armed |
| NFR-009 | Late action safety | Missed slot/deadline is skipped, never shifted into an unsafe late transmit |

## 5.2 Availability and Isolation

| ID | Requirement | Target / Verification |
|---|---|---|
| NFR-020 | Long-running RX | 24 h target; 1 h real-radio release gate without crash/leak symptom |
| NFR-021 | DSP isolation | Worker crash does not crash main; active TX is stopped and worker is restartable |
| NFR-022 | Fault recovery | CAT/audio/DSP recovery never restores armed TX automatically |
| NFR-023 | Browser reconnect | Exponential backoff; state snapshot on reconnect; no command replay |
| NFR-024 | Restart invariant | PTT off, TX disarmed, lease empty, monitor-only |

## 5.3 Security

| ID | Requirement | Target / Verification |
|---|---|---|
| NFR-030 | TLS edge | Caddy owns public 80/443; automatic TLS |
| NFR-031 | Loopback services | FastAPI, worker IPC and rigctld are not publicly bound |
| NFR-032 | Strong password | Argon2id with per-hash salt; no committed/plaintext secret |
| NFR-033 | Session cookie | Random opaque `Secure`, `HttpOnly`, `SameSite=Strict`; 30 min idle / 12 h absolute |
| NFR-034 | No URL credential | REST/WS never authenticate through query tokens |
| NFR-035 | Request validation | Mutations and WS upgrades validate configured Host and expected Origin |
| NFR-036 | Login abuse resistance | Bounded rate limit and progressive delay |
| NFR-037 | Control arbitration | Multiple sessions, exactly one renewable control lease |
| NFR-038 | Emergency stop | Any authenticated session can STOP TX without the lease |
| NFR-039 | Diagnostic re-auth | Password re-entry, five-minute sensitive-operation window and warning |

## 5.4 Safety

| ID | Requirement | Target / Verification |
|---|---|---|
| NFR-050 | Sequencer-only TX | No direct Web/DSP-to-PTT path |
| NFR-051 | Lease heartbeat | 5 s heartbeat, 15 s TTL; loss during TX stops immediately |
| NFR-052 | STOP priority | Idempotent, cancels audio/queued TX and requests PTT off immediately |
| NFR-053 | Non-blocking PTT release | No blocking confirmation loop in safety path |
| NFR-054 | Health interlocks | CAT/audio/DSP/clock fault prevents TX |
| NFR-055 | Retry bound | One initial send plus at most three retransmissions, then disarm |
| NFR-056 | Completion behavior | RR73/73 logs and disarms; no automatic next target |
| NFR-057 | Watchdog | Per-transmission and aggregate TX deadline enforced server-side |
| NFR-058 | Startup/shutdown | Best-effort PTT off on both paths; abnormal prior QSO marked aborted |

## 5.5 Compatibility and UX

| ID | Requirement | Target / Verification |
|---|---|---|
| NFR-060 | Landscape operation | Full cockpit at representative phone/tablet landscape sizes |
| NFR-061 | Portrait degradation | Observer, health, recent decodes and STOP remain; QSO start requires rotation |
| NFR-062 | Touch semantics | Explicit RX/TX/Both; selection separated from Reply |
| NFR-063 | UI safety visibility | CAT/audio/DSP, lease TTL, slot, TX/PTT and watchdog never hidden in menus |
| NFR-064 | Supported browsers | Current iOS Safari plus current desktop Safari/Chrome/Firefox |
| NFR-065 | No fake controls | Unsupported modes/features are hidden |

## 5.6 Operability and Data

| ID | Requirement | Target / Verification |
|---|---|---|
| NFR-070 | Runtime logging | Startup, health transition, decode deadline and every TX start/stop/reason |
| NFR-071 | Retention | Runtime 30 d, decode history 7 d, audit 90 d, QSO indefinite |
| NFR-072 | Canonical persistence | SQLite transactions; ADIF generated from non-void QSO records |
| NFR-073 | Audited undo | QSO undo marks void and records actor/time/reason |
| NFR-074 | Local telemetry | No third-party telemetry or cloud error reporting |
| NFR-075 | Diagnostic contents | Raw bundle may be sensitive; auth secrets and cookie values are excluded |
| NFR-076 | Health metrics | DSP latency/misses, audio overruns, drops, rig/PTT/lease/worker state |

## 5.7 Maintainability and Deployment

| ID | Requirement | Target / Verification |
|---|---|---|
| NFR-080 | Vendor immutability | No edit under `wsjtx-3.0.2/`; patches in `dsp/patched/` |
| NFR-081 | DSP boundary | All DSP calls via worker and `binding.py` lock |
| NFR-082 | Frontend structure | Vanilla modules; no inline JS logic in `index.html` |
| NFR-083 | Dual platform | Shared app config/code; LaunchAgent and systemd descriptors separate |
| NFR-084 | SDD governance | Behavior change updates affected SDD and chapter 14; guardian check clean |
| NFR-085 | LOG window & import provenance | `GET /logs/qsos` and `/logs/adif` return only the last 7 days (10k-row history must never overload the cockpit); imported QSOs carry `source='jtdx'` while live completions carry `source='live'` |
| NFR-086 | DXCC stats | `GET /api/v1/dxcc` returns ok envelope with total / entities / by_band, computed on first open and cached; any QSO write (record / import / void) marks the cache dirty so the next open recomputes (no per-open full scan, no push) |
| NFR-087 | New-DXCC auto-call | Decode messages carry `is_new_dxcc`; with setting `auto_call_new_dxcc` enabled the server auto-QSOs the first new-DXCC CQ when idle (safety-armed, no lease, never interrupts a QSO) |
