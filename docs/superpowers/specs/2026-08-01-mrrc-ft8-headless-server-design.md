# MRRC-FT8 Headless Server Design

**Date:** 2026-08-01  
**Status:** Approved design, pending written-spec review  
**Scope:** FT8 vertical slice first; FT4 follows after dual-platform FT8 acceptance  
**Governing record:** `SDD/` TeamSD chapters 1–15

## 1. Purpose

MRRC-FT8 turns the WSJT-X 3.0.2 Improved DSP core into a headless FT8/FT4 radio server. A Mac or Linux host owns the radio, audio device and DSP runtime; authenticated operators use a mobile Web interface over HTTPS.

The first release supports a normal, human-selected FT8 QSO. The operator chooses CQ or a decoded station, and the sequencer completes only that QSO. It does not continuously choose new stations, run contests, or expose Fox/Hound, SuperFox, WSPR, Q65, VHF/EME, Echo, Full Duplex or QSY Creator controls.

This design serves SC1–SC10 and NFR-001–NFR-084. Its architecture is recorded by AD-001–AD-015.

## 2. Confirmed Product Decisions

- Public Internet access through a user-owned domain and Caddy automatic TLS.
- Strong password authentication; no TOTP or passkeys in the first release.
- Multiple authenticated sessions; exactly one renewable control lease.
- Any authenticated session may issue emergency `STOP TX` without the lease.
- FT8 full path first; FT4 begins after FT8 acceptance.
- WSJT-X Improved `ft8var` OpenMP decoding is required in the first release.
- Only ordinary FT8 QSOs; no contest, Fox/Hound or SuperFox paths.
- DSP runs in a supervised worker process that exclusively owns all Fortran/OpenMP state.
- Landscape is the full mobile operating mode: waterfall left, station candidates right, runtime state machine fixed at the bottom.
- The operator selects the station. The sequencer completes one QSO and never automatically selects the next station.
- A message may be retried three times. Exhaustion disarms TX but retains the QSO context.
- RR73/73 completes and logs the QSO automatically. A short undo action creates an auditable void operation.
- Completion disarms TX. Starting another CQ or reply requires explicit operator action.
- CAT, audio, DSP, lease or controller failure immediately stops audio, releases PTT and disarms TX. Recovery never resumes transmission automatically.
- Restart restores monitoring only. It never restores PTT, armed TX or a control lease.
- macOS uses a user LaunchAgent; Linux uses systemd. Caddy owns ports 80/443; FastAPI is loopback-only. Docker is not a first-release target.
- macOS receives real-radio acceptance. Linux receives build, service, synthetic-audio and mocked-rig acceptance; real Linux hardware does not block release.
- Runtime logs retain 30 days, decode history 7 days, security/operation audit 90 days, and QSO records indefinitely.
- Diagnostic bundles are exported without redaction, only after password re-authentication and a prominent sensitivity warning. They do not require the control lease.
- No third-party telemetry or cloud error tracking.

## 3. System Architecture

```text
Mobile/Desktop Browser
  HTTPS + secure cookie + WebSocket
              |
              v
       Caddy :80/:443
              |
              v  loopback HTTP
FastAPI main process
  - authentication and sessions
  - single control lease
  - REST/WS and static PWA
  - UTC slot orchestrator
  - RX/TX audio ownership
  - rigctld client and PTT watchdog
  - QSO sequencer and persistence
              |
              | supervised local IPC
              v
DSP worker process
  - ctypes binding global lock
  - sole owner of packjt77 and ft8var/OpenMP state
  - decode/encode requests and batched results
              |
              v
wsjt_core shared library
  - Fortran shim + selected WSJT-X 3.0.2 Improved sources

FastAPI main process --TCP loopback--> rigctld --serial--> radio
FastAPI main process --PortAudio 48 kHz---------------------> radio USB audio
```

### 3.1 Ownership boundaries

| Resource | Sole owner | Rule |
|---|---|---|
| CAT serial device | `rigctld` | No project module opens the serial device. |
| Radio audio device | FastAPI main process audio engine | RX and TX streams have one lifecycle owner. |
| PTT decisions/watchdog | FastAPI main process safety controller | DSP worker can never key the radio. |
| Fortran/OpenMP state | DSP worker process | No DSP call occurs in FastAPI or an OpenMP callback into Python. |
| QSO state | Sequencer in main process | UI sends intent, not arbitrary message/PTT commands. |
| Control authority | Lease manager in main process | One lease holder; STOP TX bypasses the lease. |

The main process supervises the worker. A worker crash invalidates outstanding DSP requests, triggers the same safe-stop path as a DSP timeout, and starts a fresh worker only after TX has been disarmed. This implements AD-003, AD-005 and NFR-022.

## 4. DSP Core and ABI

### 4.1 Source policy

`wsjtx-3.0.2/` is immutable vendor reference source. The build selects a minimal source set. Any required compilation shim or patched copy lives under `dsp/` or `dsp/patched/`, with its origin and reason recorded in `AGENTS.md` and SDD chapter 11.

### 4.2 ABI shape

The C ABI uses ordinary Fortran `subroutine ... bind(C)` entry points. It exposes:

- ABI/version and capability query.
- FT8/FT4 encode and 48 kHz waveform generation.
- Standard and Improved decode configuration.
- A decode operation accepting exactly 12 kHz mono signed int16 samples.
- A fixed-capacity result array containing UTC slot, sync, SNR, `dt`, audio frequency, decoded text, AP flag and quality.

Fortran collects results into an owned array and returns the batch after OpenMP work finishes. OpenMP threads never invoke Python callbacks. Every binding entry point is additionally serialized by the global lock in `server/core/binding.py`, because `packjt77` and callsign hash tables contain mutable global state.

### 4.3 Improved decoder

The first release includes `ft8var` and supports its thread count (Auto or 1–12), cycles (1–3), sensitivity, AP, wideband DX search, duplicate handling and start profile 0–4. Profile 3 is the operational default; profile 4 is permitted only when its measured decode deadline remains safe.

The profile meanings derived from the vendor source are preserved:

| Profile | Decode start behavior |
|---|---|
| 0 | Standard early pass around 41 plus Improved pass 49 |
| 1 | Standard passes around 41/46 plus Improved pass 50 |
| 2 | Improved pass 48, approximately 13.824 s capture |
| 3 | Improved pass 49, approximately 14.112 s capture; default |
| 4 | Improved pass 50, approximately 14.400 s capture |

If a decode cannot finish before the safe TX decision cutoff, its result may still be displayed but it cannot trigger a late transmission in that slot. Deadline misses are counted and surfaced in diagnostics.

## 5. Audio and UTC Timing

### 5.1 Fixed sample domains

- Hardware RX/TX domain: 48 kHz mono.
- Decoder input: exactly 12 kHz mono signed int16.
- TX waveform: exactly 48 kHz mono.
- No other rate can cross the binding boundary.

RX uses one deterministic 48 kHz to 12 kHz conversion before buffering. The UTC-aligned 12 kHz ring feeds both decode windows and waterfall calculation. TX waveforms are generated at 48 kHz and are never routed back through the RX resampler.

### 5.2 Slot discipline

Slot identity is always `floor(epoch / TRperiod)` using UTC epoch time. FT8 uses 15 seconds and FT4 uses 7.5 seconds. Relative timers may wake tasks but never define slot identity.

The orchestrator records capture cutoff, worker dispatch, decode completion, TX decision and actual audio/PTT timestamps. A missed cutoff skips the unsafe action rather than transmitting late.

## 6. QSO Sequencer

### 6.1 State model

```text
MONITOR
  -> SELECTED or CQ_READY
  -> TX_ARMED
  -> REPLYING / REPORT / ROGER_REPORT / ROGERS / SIGNOFF
  -> COMPLETED
  -> MONITOR (TX disarmed)

Any active state
  -> RETRY_EXHAUSTED | STOPPED | FAULT | ABORTED_RESTART
  -> MONITOR (TX disarmed; context retained where applicable)
```

Tx1–Tx6 retain the WSJT-X meanings: calling/replying, report, roger-report, rogers, signoff and CQ/custom calling. The parser accepts standard 77-bit FT8 messages required for a normal QSO and rejects unsupported contest/Fox/Hound flows.

### 6.2 Operator intent

Selecting a decode only highlights the station and fills the review panel. `Reply` is a separate explicit action. It sets the DX call/grid, correct RX offset and odd/even relationship, then arms the one-QSO sequencer if the session owns the control lease.

Each unanswered message has one initial send and at most three retransmissions (four sends total). On exhaustion, TX is disarmed, PTT is released, and the UI retains the target and last message for manual review.

### 6.3 Completion and logging

Receipt of RR73/73 completes the QSO, persists it, and disarms TX. The UI offers a 30-second undo. Undo does not silently delete history: it marks the canonical record void, records actor/time/reason in the audit log, and regenerates the user ADIF export from non-void records.

## 7. PTT Safety

All transmission passes through sequencer intent and the central safety controller. There is no direct Web-to-PTT or DSP-to-PTT path.

Safety layers are:

1. Authentication and Origin/Host validation.
2. Valid control lease for all transmit-starting actions.
3. Sequencer and slot eligibility checks.
4. Audio/CAT/DSP health interlocks.
5. Per-transmission deadline and PTT watchdog.
6. Lease heartbeat/dead-man: heartbeat 5 s, TTL 15 s.
7. Universal authenticated `STOP TX` path.
8. Restart invariant: monitoring only, PTT off, TX disarmed.

`STOP TX` immediately cancels audio, requests PTT off without a blocking confirmation loop, cancels queued TX work and disarms the sequencer. It is idempotent and higher priority than all normal commands.

## 8. Public Security and Control Lease

Caddy is the only public listener and terminates TLS. FastAPI binds `127.0.0.1` or `::1`; the DSP worker and rigctld are never exposed publicly.

Passwords are stored with Argon2id using a per-hash salt. Session IDs are random opaque values in `Secure`, `HttpOnly`, `SameSite=Strict` cookies. WebSockets authenticate with the cookie; credentials are never placed in URL query strings. Mutating HTTP and all WebSocket upgrades validate the expected Origin and configured Host. A session has a 30-minute idle timeout and 12-hour absolute lifetime. Login uses bounded rate limiting and progressive delay. Diagnostic re-authentication is valid for five minutes and only for the requested sensitive operation class.

Multiple sessions may observe concurrently. A session explicitly acquires the control lease over its state/control channel, renews it every 5 seconds, and loses it after 15 seconds without a valid heartbeat. If that lease holder's control WebSocket disconnects during active or queued TX, emergency stop runs immediately rather than waiting for TTL. The lease is never restored after a server restart.

Any authenticated session may use `STOP TX`. Diagnostic export requires recent password re-authentication but not the lease.

## 9. API and Stream Model

REST handles snapshots, configuration, logs, session/lease operations and idempotent commands. Separate WebSockets isolate traffic classes:

- State/safety: lease, rig/audio/DSP health, slot phase, TX/PTT, sequencer state.
- Decode events: immutable per-slot decode batches and candidate updates.
- Waterfall: bounded binary spectrum frames.

Each connection has bounded queues. A slow waterfall client drops old spectrum frames. It cannot delay safety state, lease heartbeats, decode events or STOP TX. Commands carry an idempotency key and receive an accepted/rejected response with the current state revision.

## 10. Landscape Mobile Web

Full operation requires landscape orientation. The layout follows the efficient qFT8 spatial model while retaining WSJT-X semantics and MRRC safety:

- Left: live peak spectrum and waterfall, UTC/slot annotations, decoded labels and explicit RX/TX markers.
- Right: Band Activity or Rx Frequency station candidates with callsign, DXCC/grid, QSO phase, SNR, `dt`, distance, offset, recent message and `Reply`.
- Bottom fixed runtime bar: monitor/TX status, current and next Tx1–Tx6 message, even/odd, FT8/FT4, band, lease TTL, watchdog, Enable TX and independent red STOP TX.

Portrait mode is a deliberate degraded view: health/state, selected QSO, recent decodes and STOP TX remain available, but starting or reconfiguring a QSO prompts the user to rotate to landscape.

Touch replaces desktop modifiers:

- Explicit `Set RX`, `Set TX` and `Set Both` modes replace click/Shift-click/Ctrl-click.
- Tapping a decode selects it; tapping `Reply` performs the WSJT-X double-click semantics.
- Long press opens non-transmit helpers such as copy, hide callsign and history.
- Hold TX Frequency is an explicit operating control.

Frequently used controls remain on the Live screen. Low-frequency functions are grouped under:

- Operating
- Decoder
- Waterfall
- Filters & Highlighting
- Station & Radio
- Logs & Diagnostics
- Help & About

Unsupported first-release features are hidden, not rendered as inert controls.

## 11. Persistence, Diagnostics and Retention

SQLite is the canonical local store for QSO records, settings metadata, session/audit metadata and bounded decode history. ADIF is generated/exported from canonical non-void QSO records. Critical writes use transactions and fsync-appropriate durability.

Retention:

- Runtime logs: 30 days.
- Decode history: 7 days.
- Security and operation audit: 90 days.
- QSO records: no automatic expiry.

The health page shows DSP latency/deadline misses, audio overruns, dropped waterfall frames, UTC slot state, worker restarts, rig connection and PTT/watchdog state.

A diagnostic bundle contains raw configuration, logs, audit, health metrics and device paths. It is intentionally not redacted. Export requires password re-authentication and an explicit warning that the archive may contain domains, IP addresses, callsigns, grids, device paths and session-adjacent metadata. Authentication secrets and raw cookie values are never exported. No monitoring data is sent to third parties.

## 12. Deployment

### macOS

- User-level LaunchAgent starts the FastAPI supervisor so PortAudio permissions remain associated with the logged-in user.
- Caddy runs as the TLS reverse proxy.
- Real FT-710, rigctld, audio and PTT acceptance is required.

### Linux

- systemd starts the FastAPI supervisor with explicit device/group access.
- Caddy runs as the TLS reverse proxy.
- Build, service lifecycle, synthetic audio and mocked rigctld acceptance are required for the first release.

Both platforms share application configuration and code. Platform service descriptors are separate. The service refuses to start TX-capable mode if required safety configuration is absent.

## 13. Failure and Restart Behavior

| Failure | Required behavior |
|---|---|
| Browser/WS controller disconnect | Immediate stop during TX; lease expires; no queued command replay. |
| Control lease expires | Immediate stop and disarm. |
| rigctld/CAT failure | Stop audio, request PTT off, disarm, show fault. |
| Audio underflow/device loss | Stop audio/PTT, disarm, require manual recovery. |
| DSP timeout/crash | Stop/disarm, invalidate result, restart worker, require manual re-arm. |
| Main process restart | Best-effort PTT-off startup, mark active QSO `ABORTED_RESTART`, enter monitor-only. |
| Slow Web client | Drop waterfall frames only; preserve safety/state delivery. |
| Clock step or unsafe offset | Inhibit TX until UTC timing is stable and operator re-arms. |

No recovery path automatically resumes a QSO or restores Enable TX.

## 14. Verification Strategy

- `ft8sim` and later `ft4sim` known-message/SNR decode regression.
- DSP ABI, capacity, global serialization and worker-crash tests.
- Assertions that decoder input is 12 kHz int16 mono and TX is 48 kHz.
- Epoch-injected slot boundary, late-decode and clock-step tests.
- Sequencer transition, three-retry, completion, auto-log and auditable-undo tests.
- REST/WS authentication, Origin/Host, session, lease, heartbeat and universal STOP tests.
- Bounded-queue tests proving slow waterfall cannot block state/safety.
- Fault injection for CAT, audio, DSP, browser disconnect and restart.
- Responsive UI checks at representative phone/tablet landscape sizes; portrait degraded-mode checks.
- macOS real-radio end-to-end acceptance.
- Linux build/service/synthetic-audio/mocked-rig acceptance.

## 15. Delivery Milestones

1. FT8 DSP ABI, Improved decoder and synthetic-signal regression.
2. Audio/UTC engine, rigctld boundary, sequencer and safety controller.
3. Authentication, lease, REST/WS and landscape Web vertical slice.
4. macOS real FT-710 end-to-end acceptance.
5. Linux service and simulated acceptance.
6. FT4 extension using the same boundaries and safety model.

Each behavior change updates the affected SDD chapters and `SDD/14-version-history.md`. The vendor directory remains unchanged.

## 16. Deferred Work

- Continuous qFT8-style automatic target selection.
- Contest, Fox/Hound and SuperFox sequences.
- TOTP/passkey authentication.
- Native mobile applications.
- Docker hardware deployment.
- Third-party telemetry, remote Prometheus or cloud error tracking.
- Linux real-radio certification beyond user-led validation.

## 17. Source Research

- WSJT-X 3.0.2 Improved vendor source and user guide in `wsjtx-3.0.2/`.
- qFT8 2.07 public website, Chinese user manual, Stations/Bottom bar screenshots and remote Web screenshots at `https://qft8.com/`.
- qFT8 informed spatial layout and station-card density only. MRRC-FT8 does not copy qFT8's VOX ownership, unauthenticated-port assumptions, continuous AUTO targeting or last-writer-wins control behavior.

## 18. SDD Traceability

| Design area | Governing references |
|---|---|
| DSP source, worker, lock and Improved decoder | AD-002, AD-003, AD-005; NFR-001, NFR-007, NFR-021, NFR-081; SC1, SC3; R1–R4 |
| Audio and UTC timing | AD-004, AD-006; NFR-004, NFR-006–NFR-009; R5, R6 |
| Sequencer and PTT safety | AD-007, AD-008, AD-012; NFR-050–NFR-058; UC-003–UC-008; SC2, SC4–SC6 |
| Public security and lease | AD-009, AD-010; NFR-030–NFR-039; UC-001, UC-002, UC-006; R7 |
| Web streams and landscape UI | AD-011, AD-013; NFR-060–NFR-065; SC7, SC8; R8 |
| Persistence and diagnostics | AD-014; NFR-070–NFR-076; UC-009, UC-010; R10 |
| Deployment | AD-015; NFR-083; SC9, SC10; R9 |
| Governance | NFR-080, NFR-082, NFR-084; SDD chapter 14 |
