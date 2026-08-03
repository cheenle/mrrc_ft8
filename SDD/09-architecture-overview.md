# 9. Architecture Overview

## 9.1 Process Topology

The FastAPI main process owns all safety-critical state and spawns/supervises one DSP Worker. `rigctld` may be externally managed or supervised locally, but it is always reached over loopback TCP. Caddy is an independent edge service. Main–Worker control uses bounded Protocol v1 JSON frames over a duplex pipe; exact RX/TX arrays remain in parent-owned named shared memory and never enter the control frame.

## 9.2 Receive Pipeline

1. Open configured 48 kHz mono input.
2. Convert once to 12 kHz int16.
3. Write samples into a UTC-indexed ring addressed by absolute sample index
   (`X % capacity`); eviction advances the base boundary without moving data.
4. Produce bounded waterfall frames spanning only the ~3 kHz FT8 passband
   (`DISPLAY_BANDWIDTH_HZ`); the client maps those bins across the full canvas.
5. At the configured profile cutoff, copy the decode window into a fixed
   parent-owned shared-memory segment and send only its exact descriptor.
6. Worker validates generation before opening the segment, maps a read-only
   12 kHz int16 view, serializes the DSP call, closes without unlinking, and
   returns one bounded immutable batch.
7. Main validates slot/deadline, updates candidate state and broadcasts immutable decode events.

Improved profiles map exactly to `41+49`, `41+46+50`, `48`, `49` and `50`
half-symbol pass sequences for profiles 0–4. Improved frequency work is divided
into the requested 1–12 adjacent inclusive bands, while all callbacks remain in
Fortran and the final batch performs stable exact decode-key deduplication.
The runtime must form exactly the requested OpenMP team; otherwise the ABI
returns `WSJT_E_INTERNAL=8` and copies no result records. Each thread receives
its own slot work buffer, downsample cache, FFTW plan registry and cycle 2/3
scratch. Subtraction is deliberately band-local subtraction: unlike the
upstream GUI's unsynchronized shared-buffer subtraction, one thread cannot
change another band's input, making the headless batch deterministic. FFTW
plans remain cached for the DSP Worker lifetime and are reclaimed at process
exit.

Ordinary decode callbacks atomically clear the request's A8 gate only for a
decode strictly within 3 Hz of Rx. After all bands reach the A8 barrier, exactly
the deterministic band owner that contains Rx reads the gate and may attempt
A8; band 1 owns an out-of-window Rx fallback. The OpenMP append order remains
scheduling-dependent, so result order is unspecified and consumers compare or
group decode keys rather than sorting inside the ABI.

## 9.3 Transmit Pipeline

1. Accept explicit CQ/Reply intent from the lease holder.
2. Validate auth, lease, state revision, sequencer transition and interlocks.
3. Give the worker an exact parent-owned TX shared-memory descriptor; the worker
   packs/encodes directly into its 606,720-sample 48 kHz float32 view and returns
   metadata only.
4. Revalidate lease/health/slot immediately before keying.
5. Key PTT through the safety controller, play audio, and release after the bounded window.
6. Publish actual start/stop reason and disarm on completion/fault.

The slot-parity TX driver pulls at most one sequencer message per eligible slot
(gating on the sequencer's per-QSO `tx_phase`, UC-003). The decision is
provisional (I9): when the sequencer is idle at slot start, the driver keeps the
slot's TX window open until `TX_DECISION_CUTOFF_SECONDS` (5.0), polling so a
Reply transmits as soon as it is armed; a fit guard refuses any start past
~2.4 s into the slot (the fixed 12.64 s waveform must fit a 15 s slot), deferring
to the next eligible slot rather than overrunning the boundary (an overrun is
undecodable at the partner and deafens the next slot's RX).

## 9.4 Timing

FT8 uses 15 s slots; FT4 uses 7.5 s slots. Profile 3 is the initial Improved FT8 default. The orchestrator records capture, dispatch, result, decision, PTT and audio timestamps. Two distinct cutoffs bound the loop:

- **Decode lateness** (`DEFAULT_DECISION_CUTOFF_SECONDS = 2.5`, orchestrator): a batch that finishes more than 2.5 s after the slot ended is display-only for that slot, never fed to the sequencer — a late result cannot trigger a late TX.
- **TX decision window** (`TX_DECISION_CUTOFF_SECONDS = 5.0`, tx driver): how long after the slot starts a manual Reply may still be armed; the fit guard caps the actual transmit at ~2.4 s into the slot (12.64 s waveform vs 15 s slot), deferring later arming to the next eligible slot.

Decoder threads default to Auto = `clamp(cpu_count - 1, 1, 12)` (I9, measured on Apple M2; the opt-in benchmark re-verifies both per supported CPU).

## 9.5 Web Delivery

- State/safety stream is low volume, revisioned and never intentionally dropped.
- Decode batches are immutable, ordered by slot and bounded by history policy.
- Waterfall is lossy under pressure; each client keeps only the newest useful frames.
- Reconnect begins with an authoritative snapshot. Commands are never replayed from browser queues.

## 9.6 Failure Containment

Malformed/oversize control frames and pipe corruption terminate the Worker
nonzero for supervisor detection. Valid stale-generation, shared-memory,
configuration and native-status failures return one sanitized correlated error
and leave the loop usable. Worker crash, IPC timeout, rig failure, audio error,
clock fault, lease loss and controller disconnect converge on one safe-stop
operation. Restarted components return to monitor-only; no component independently
resumes a QSO.

## 9.7 UI Architecture

The Live cockpit maintains the entire frequent-operation loop in landscape. Settings and auxiliary functions are routed to menus. Static HTML contains no inline application logic; modules own state, stream clients, waterfall drawing, candidate cards, sequencer presentation and safety controls.
