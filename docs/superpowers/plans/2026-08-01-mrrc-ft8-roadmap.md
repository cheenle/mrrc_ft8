# MRRC-FT8 Delivery Roadmap

**Approved baseline:** `docs/superpowers/specs/2026-08-01-mrrc-ft8-headless-server-design.md`

**Purpose:** Sequence the approved design into independently accepted vertical phases without weakening the fixed DSP, timing, ownership, security, lease, or PTT invariants.

**Execution rule:** Each phase gets its own detailed implementation plan before code for that phase is changed. A later phase may start only after the preceding exit gate is recorded in `SDD/14-version-history.md`.

**Governing references:** SC1–SC10, NFR-001–NFR-084, AD-001–AD-015, UC-001–UC-010, R1–R10, and I8–I11.

---

## Product slice and dependency order

```text
Phase 1 FT8 DSP ABI/Worker (M1)
          |
          v
Phase 2 Audio/UTC/Rig/Sequencer/PTT (M2)
          |
          v
Phase 3 Auth/Lease/API/WS/Landscape Web (M3)
          |
          +-------------------+
          v                   v
Phase 4 macOS real radio   Phase 5 Linux simulated
          +-------------------+
                    |
                    v
             FT8 V1.0 acceptance
                    |
                    v
             Phase 6 FT4 extension
```

Phase 4 and Phase 5 may run in parallel only after Phase 3 passes. Phase 6 starts only after both platform gates pass.

## Interfaces locked for all phases

| Boundary | Contract |
|---|---|
| Decode audio | Exactly 180,000 signed int16 mono samples at 12,000 Hz per complete FT8 slot buffer; the request carries sample rate and shape and both worker and binding reject mismatches. |
| TX waveform | Exactly 606,720 mono float32 samples at 48,000 Hz for the 79-symbol FT8 waveform; the audio engine applies device conversion only outside the DSP boundary. |
| Slot identity | `slot_id = floor(epoch_seconds / tr_period)`; FT8 `tr_period=15.0`, FT4 `tr_period=7.5`. |
| DSP ownership | Only the supervised DSP Worker imports `server/core/binding.py`; only `binding.py` loads `wsjt_core`; every ABI call holds one process-global lock. |
| Worker request | Versioned typed local IPC with `generation`, `request_id`, `slot_id`, operation, deadline, config and shared-memory audio descriptor. |
| Worker response | Versioned typed response with matching generation/request/slot, status, elapsed time, overflow/deadline flags and an immutable decode batch or waveform descriptor. |
| Failure | Timeout, EOF, protocol mismatch, generation mismatch or worker exit invalidates all in-flight requests and reports a DSP fault. Restart produces a new generation and monitoring-only state. |
| Radio | Only `rigctld` opens serial; application code uses loopback Hamlib TCP. |
| TX authority | Only the sequencer may request TX; only the safety controller may start/cancel audio and set PTT. |
| Public control | Caddy owns public 80/443; FastAPI/rigctld are loopback-only; authentication is a Secure HttpOnly SameSite=Strict cookie. |
| Lease | One holder, heartbeat 5 seconds, TTL 15 seconds; any authenticated observer can execute idempotent STOP TX without the lease. |

## Phase 1 — FT8 DSP ABI, Improved decoder and supervised Worker

**Detailed plan:** `docs/superpowers/plans/2026-08-01-ft8-dsp-worker.md`

**Delivers:** A macOS/Linux-buildable `wsjt_core` shared library, standard and Improved FT8 batch decoding, 48 kHz encoding, a locked ctypes boundary, generation-tagged worker IPC, crash/timeout recovery, synthetic regression and profile/thread measurements.

**Requirements:** SC1, SC3; NFR-001, NFR-002, NFR-007, NFR-021, NFR-022, NFR-076, NFR-080, NFR-081, NFR-084; AD-002, AD-003, AD-005; R1–R4; I8, I9.

**M1 exit gate:**

- `cmake -S dsp -B dsp/build -DCMAKE_Fortran_COMPILER=gfortran-mp-13 && cmake --build dsp/build -j` succeeds on the development Mac.
- ABI capability/version, struct sizes, invalid-rate rejection and concurrent lock tests pass.
- `CQ K1ABC FN42` round-trips through encode and standard plus Improved decode from a deterministic 12 kHz synthetic buffer.
- Worker crash and timeout tests return a DSP fault, reject stale responses, create a new generation and never crash the test process.
- Profile 0–4 and thread 1–12 benchmark data is written to `artifacts/dsp-benchmark.json`; profile 3 must meet the recorded safe decision cutoff or M1 is no-go.
- I8 is closed with protocol version 1, one 360,000-byte shared-memory RX buffer per in-flight decode, result capacity 256 and a 64 KiB maximum control frame.
- I9 is closed with measured platform-specific Auto thread selection and safe cutoff; no estimate is promoted to an operational default.
- Guardian check is clean and SDD chapters 9, 11, 13, 14 plus patch registers are synchronized.

## Phase 2 — Audio/UTC engine, rigctld and PTT-safe one-QSO sequencer

**Detailed-plan filename:** `docs/superpowers/plans/2026-08-01-ft8-engine-safety.md`

**Plan authoring input:** Phase 1's measured profile/cutoff table and final Worker request/response types.

**Delivers:** 48 kHz RX/TX ownership, deterministic 4:1 RX conversion, UTC ring/cutoffs, worker dispatch, waterfall frames, async rigctld client, one-QSO state machine, SQLite/ADIF, priority STOP and watchdog safety.

**Requirements:** SC2, SC4, SC6, SC7; NFR-003–NFR-020, NFR-022–NFR-030, NFR-050–NFR-059 (including NFR-051, NFR-052, NFR-055 and NFR-056), NFR-070–NFR-075; AD-004, AD-006–AD-008, AD-012, AD-014; UC-001–UC-007 including UC-005 and UC-006; R5–R7; `SDD/15-ptt-safety-architecture.md`.

**Files reserved:**

- `server/engine/orchestrator.py`: epoch-floor slot IDs, capture/decision deadlines and late-result display-only classification.
- `server/engine/audio_rx.py`: 48 kHz mono capture, one 4:1 conversion and 12 kHz UTC ring.
- `server/engine/audio_tx.py`: bounded 48 kHz playback and immediate cancellation.
- `server/engine/waterfall.py`: spectrum computation only; no control-path dependency.
- `server/engine/rig.py`: async rigctld TCP protocol; never serial.
- `server/engine/safety.py`: sole PTT/audio cancellation authority and watchdog.
- `server/engine/msgparse.py`: standard non-contest message parser.
- `server/engine/sequencer.py`: one initial send plus three retransmissions, retained context on exhaustion/fault, RR73/73 completion and disarm.
- `server/engine/repository.py`: SQLite QSO/decode/audit storage and retention.
- `server/engine/adif.py`: deterministic non-void ADIF export.

**M2 exit gate:**

- Synthetic 48 kHz capture produces exactly 12 kHz int16 decode windows with no second conversion.
- Clock jump, late cutoff, audio error, CAT error, worker timeout and process restart all converge on monitor-only, PTT-off, TX-disarmed state.
- One standard QSO passes from explicit CQ/Reply through RR73/73, automatic log and auditable 30-second void.
- Retry test proves four total sends, then disarm with retained target/message context.
- STOP preempts queued/active TX without a blocking PTT confirmation loop.

## Phase 3 — Authentication, control lease, API/streams and mobile landscape Web

**Detailed-plan filename:** `docs/superpowers/plans/2026-08-01-ft8-web-control.md`

**Plan authoring input:** Phase 2 engine intents, health snapshot and safety event model.

**Delivers:** FastAPI composition, Argon2id authentication, secure sessions, Host/Origin enforcement, one dead-man lease, idempotent REST commands, separated bounded WebSockets, offline shell and qFT8-inspired landscape cockpit with low-frequency settings moved to menus.

**Requirements:** SC5, SC8; NFR-031–NFR-049, NFR-060–NFR-069, NFR-077–NFR-079, NFR-082; AD-009, AD-010, AD-011, AD-013, AD-015; UC-001–UC-010; R8–R10.

**Files reserved:**

- `server/main.py`, `server/config.py`
- `server/web/auth.py`, `server/web/lease.py`, `server/web/api.py`, `server/web/ws.py`
- `server/web/static/index.html`, `styles.css`, `app.js`, `state.js`, `waterfall.js`, `stations.js`, `runtime-bar.js`, `menus.js`, `manifest.webmanifest`, `sw.js`

**M3 exit gate:**

- Login/session expiry/rate-limit and Secure HttpOnly SameSite=Strict cookie tests pass.
- Host/Origin rejection covers mutating HTTP and WebSocket upgrades; no URL token path exists.
- Multiple observers coexist, exactly one lease controls TX, 5-second renewal and 15-second expiry work, and controller WS loss during TX immediately stops.
- Any authenticated observer can STOP without lease; unauthenticated requests cannot.
- Slow waterfall consumers drop old waterfall frames without delaying state, heartbeat, decode batches or STOP.
- Landscape exposes runtime/safety state permanently; portrait remains observe/STOP only; setup/auxiliary/low-frequency controls live in menu surfaces.

## Phase 4 — macOS service deployment and real FT-710 acceptance

**Detailed-plan filename:** `docs/superpowers/plans/2026-08-01-macos-ft710-acceptance.md`

**Plan authoring input:** Passed M3 build, measured I9 settings and measured I10 FT-710 audio/PTT lead/lag.

**Delivers:** User LaunchAgent units, Caddy configuration, protected environment/configuration, log rotation, backup/restore and real FT-710 safety acceptance.

**Requirements:** SC9; NFR-005, NFR-011–NFR-020, NFR-070–NFR-079, NFR-083; AD-001, AD-008, AD-011, AD-015; I10.

**Exit gate:** Launch/login restart, TLS renewal configuration, rigctld serial ownership, USB audio, normal QSO, STOP from observer, lease loss, browser disconnect, DSP crash, audio failure and service restart are witnessed with PTT physically released and no automatic TX resumption.

## Phase 5 — Linux systemd and simulated acceptance

**Detailed-plan filename:** `docs/superpowers/plans/2026-08-01-linux-simulated-acceptance.md`

**Plan authoring input:** Passed M3 build and the Linux baseline chosen in I11 without expanding V1.0 to an unbounded distribution matrix.

**Delivers:** systemd user/system service choice, Caddy, tmpfiles/log rotation, mock rigctld, synthetic audio source and reproducible build/service tests.

**Requirements:** SC10; NFR-011–NFR-020, NFR-070–NFR-084; AD-001, AD-008, AD-011, AD-015; I11.

**Exit gate:** Clean Linux build, service start/restart, loopback-only listeners, synthetic decode, mocked CAT/PTT, lease/STOP/fault injection, persistence and retention tests pass. Linux real hardware is explicitly not a release blocker.

## Phase 6 — FT4 extension after FT8 acceptance

**Detailed-plan filename:** `docs/superpowers/plans/2026-08-01-ft4-extension.md`

**Plan authoring input:** Both platform gates passed and FT8 interfaces frozen at protocol version 1.

**Delivers:** FT4 encode/decode capability, 7.5-second epoch-floor scheduling, FT4 waveform/audio regression, mode switch intent and UI mode visibility without changing FT8 safety semantics.

**Requirements:** NFR-006–NFR-009, NFR-050–NFR-059, NFR-081; AD-002–AD-007, AD-012.

**Exit gate:** `ft4sim` regression, ABI capability compatibility, 7.5-second slot/deadline tests, mode-switch disarm test, both platform service suites and all FT8 regressions pass.

## Cross-phase release gate

The release is rejected if any of these remain true:

- A decode or TX rate/shape invariant is unchecked at a process boundary.
- Any DSP call can occur in FastAPI, outside `binding.py`, or without its global lock.
- Any code other than rigctld can open the CAT serial device.
- Any path can key PTT outside `safety.py`, or STOP can wait behind ordinary commands.
- Restart, fault recovery or lease reacquisition can automatically resume TX.
- A public listener bypasses Caddy, a credential appears in a URL, or a WebSocket skips Host/Origin/session validation.
- M1 synthetic DSP, M2 safety, M3 security/backpressure, macOS real-radio, or Linux simulated gates are not green.
- Tests, `tests/README.md`, affected SDD chapters and `SDD/14-version-history.md` disagree with verified behavior.
