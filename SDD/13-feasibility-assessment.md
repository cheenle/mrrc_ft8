# 13. Feasibility Assessment

## 13.1 Overall Assessment

The design is feasible because vendor DSP, gfortran, fftw3f, FastAPI, rigctld and PortAudio are available. The largest risks are Improved decoder deadline, Fortran/OpenMP isolation, audio/clock correctness and public TX safety. The vertical-slice order attacks these before UI polish.

## 13.2 Risks

| ID | Risk | Mitigation / Evidence |
|---|---|---|
| R1 | macOS/Linux Fortran/OpenMP build/link incompatibility | M1 minimal source build, exact four-export ABI smoke test, generated ELF map regression, documented toolchain |
| R2 | Improved profile misses TX decision deadline | Profile 3 default, Auto threads, latency histogram, late result display-only |
| R3 | packjt77/ft8var mutable state races | One worker owner, binding lock, exact OpenMP team check, thread-private slot/FFT/cycle state, deterministic A8 owner, batched Fortran return and repeated parallel-region regression |
| R4 | Worker crash or native fault | Separate process, generation-tagged IPC, fail-closed stop/restart |
| R5 | Audio device loss, overrun or wrong rate | One audio owner, explicit assertions, fault injection, real-radio acceptance |
| R6 | UTC step/drift causes invalid slot transmission | Epoch slot math, clock health interlock, manual re-arm |
| R7 | Public attacker or concurrent client starts TX | Caddy TLS, strong auth, Origin/Host, rate limits, one lease, universal STOP |
| R8 | Browser backpressure delays safety traffic | Separate bounded streams; waterfall drops only |
| R9 | macOS background service cannot access audio | User LaunchAgent and explicit permission acceptance |
| R10 | Diagnostic bundle leaks sensitive local data | Password re-auth, prominent raw-data warning, user-authorized local export |

Task 5's direct-A8 hook repeatedly decodes a clean fixed message and the atomic
near-Rx gate has a native threshold/concurrency probe. A deterministic weak
direct-A8 fixture could not be reproduced across fresh library processes, so
that weak direct-A8 fixture remains a strict expected failure and a documented
test-evidence limitation. It is not an architecture blocker; it must not be
misrepresented as weak-signal sensitivity coverage.

## 13.3 Assumptions

| ID | Assumption |
|---|---|
| A1 | Operator owns a domain whose DNS points to the Caddy host |
| A2 | FT-710 and supported host expose stable 48 kHz USB audio |
| A3 | rigctld supports required frequency/mode/PTT operations for the target radio |
| A4 | Host clock is normally synchronized closely enough for FT8; unsafe state is detectable |
| A5 | Apple Silicon/macOS has sufficient CPU for profile 3 Improved decoding with OpenMP |
| A6 | One personal station and one simultaneous QSO are sufficient for V1.0 |

## 13.4 Resolved Issues

- I1 public access: domain + Caddy TLS.
- I2 authentication: strong password and secure cookie; no TOTP/passkey V1.0.
- I3 concurrency: many sessions, one control lease.
- I4 DSP architecture: supervised worker with `ft8var` and batch results.
- I5 UI: landscape qFT8 spatial model, WSJT-X semantics, MRRC safety.
- I6 QSO automation: human target, one-QSO sequencer, three retries.
- I7 platforms: macOS real hardware, Linux simulated acceptance.
- I8 IPC representation: Protocol v1 uses bounded exact-schema UTF-8 JSON
  control frames and fixed parent-owned RX/TX shared-memory descriptors. Fresh
  spawned-Worker encode/decode proves the capacities; Darwin's uniquely
  observable page-rounded allocation is validated without changing logical
  bytes or mapped shape.
- I9 timing (measured 2026-08-01, Apple M2, 8 logical CPUs 4P+4E,
  gfortran-mp-13 Release, opt-in benchmark `MRRC_FT8_I9_BENCHMARK=1` on a busy
  five-signal noisy synthetic slot through the real supervised Worker): profile
  3 with Auto threads decodes in 0.226–0.274 s wall; native decode time equals
  wall within IPC noise, and the worst spot check (profile 3, Auto threads,
  cycles 3) stays under 0.69 s. Thread scaling saturates at 4–6 threads and
  oversubscription to 12 threads is measurably slower. V1.0 Auto thread policy
  is therefore `clamp(cpu_count - 1, 1, 12)` — never oversubscribed, 0.26 s on
  M2 — and the provisional TX decision cutoff is slot end + 2.5 s (about 9x the
  measured headline worst case, leaving 12.5 s for decision plus the I10
  PTT/audio lead). The benchmark gate re-verifies both parameters per
  supported CPU.

## 13.5 Open Issues

| ID | Issue | When resolved |
|---|---|---|
| I10 | FT-710 PTT measured (2026-08-01, Mac mini M2, C-Media USB audio, 5 W): priority STOP releases PTT in 0.26–0.31 s; a 2 s CAT-keyed transmission closes 0.8–0.9 s after audio ends. Residual uncertainty: audio-lead at key-down not yet instrumented | macOS acceptance |
| I11 | Linux distributions and audio backend matrix beyond baseline | After V1.0 |

Open issues may tune implementation parameters but cannot weaken the fixed ownership, rate, lease or safety decisions.

## 13.6 Go/No-go Gates

- M1 no-go if Improved decoder cannot build or pass synthetic regression.
- M2 no-go for TX if 12/48 kHz assertions, STOP priority and fault interlocks are not tested.
- M3 no-go for public exposure if cookie/Origin/Host/lease tests fail.
- Release no-go if macOS real-radio safety scenarios or Linux simulated service gate fail.
