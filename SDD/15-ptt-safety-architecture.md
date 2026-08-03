# 15. PTT Safety Architecture

## 15.1 Safety Objective

No browser, DSP result, retry loop, reconnect or restart may cause unowned or indefinite transmission. A single central safety controller in the main process is the only PTT authority.

## 15.2 Layered Authorization

1. TLS terminates at Caddy.
2. Session authentication and Host/Origin validation succeed.
3. A transmit-starting action has the current control lease.
4. Command idempotency and state revision are valid.
5. Sequencer transition and message type are supported.
6. CAT, audio, DSP and clock interlocks are healthy.
7. UTC slot and deadline are eligible.
8. Safety controller keys PTT and starts bounded audio.

The authority chain is rechecked immediately before PTT; earlier approval is insufficient if state changed.

## 15.3 STOP TX

STOP is accepted from any authenticated session without a lease. It is idempotent and priority-routed. It:

1. Cancels pending and active audio playback.
2. Cancels queued TX work and disarms the sequencer.
3. Sends PTT-off through rigctld without a blocking verify loop.
4. Publishes a terminal state and audit event.

If rigctld is unavailable, the system remains faulted/disarmed and repeats best-effort PTT-off on reconnect before allowing any new TX.

The stop reason maps to sequencer semantics: operator-originated reasons (`manual`, the API's `api:<actor>`) disarm as MANUAL; every other reason (dead-man, watchdogs, shutdown) disarms as FAULT, so downstream consumers such as the automatic CQ loop see the true origin and never re-arm after a system stop.

## 15.4 Dead-man and Watchdogs

- Lease heartbeat is every 5 s; TTL is 15 s.
- Controller WS disconnect during active/queued TX invokes STOP immediately, without waiting for TTL.
- Lease expiry invokes STOP.
- Per-transmission audio/PTT duration is bounded by mode waveform plus configured small margins.
- An aggregate TX watchdog prevents indefinite sequencer/retry activity.

A deliberate lease RELEASE does not run the dead-man STOP; the dead-man path only covers expiry and controller disconnect. RELEASE instead disarms manually: the sequencer stops and the armed flag drops, without touching PTT, so an in-flight waveform plays out and its transmission ends with the normal PTT-off finalize.

## 15.5 Fault Matrix

| Trigger | Immediate action | Recovery |
|---|---|---|
| CAT/rigctld fault | Cancel audio, PTT-off request, disarm | Repair, verify RX, manual lease/re-arm |
| Audio underflow/device loss | Cancel/close stream, PTT off, disarm | Reopen in monitor, manual re-arm |
| Degraded capture session (hot band, zero decodes for 4 consecutive slots) | Latch AUDIO, disarm, auto-reopen the capture stream in monitor state (≤3 bounces per episode) | Operator verifies RX, clears fault, manual re-arm |
| DSP timeout/crash | Disarm/stop, invalidate result | Restart worker, manual re-arm |
| Clock unsafe | Cancel queued TX and disarm | Stable clock, manual re-arm |
| Lease/controller loss | STOP | Acquire new lease, manual re-arm |
| Main restart | Startup PTT off, no lease, monitor-only | Manual operation |

Faults latch per interlock: re-reporting an already-faulted interlock is a no-op, and only `clear_fault` clears it, so a dead Worker failing every slot faults once instead of once per slot. The DSP interlock is wired at both ends of the Worker path: TX-driver encode errors and orchestrator decode errors both call `report_fault(DSP)`. A `TxRefused` from the safety controller is not re-reported: refusal or abort by the safety authority (STOP-cancelled playback, disarm, watchdog, already-latched interlock) is the safety system working as designed, and genuine CAT/audio faults latch inside `transmit()` before it raises.

The sole recovery channel is the lease-gated, idempotent `/operation/clear-fault` endpoint: with no body it clears every latched interlock, with `{"interlock": "<name>"}` only that one, and an unknown name is rejected. Clearing is a deliberate operator acknowledgment that the cause is repaired; it never re-arms, and any re-arm still crosses the full §15.2 chain through the operator's CQ/Reply path.

## 15.6 Sequencer Safety

The sequencer allows one initial send plus at most three retransmissions of a QSO exchange message, then disarms; CQ calling repeats until answered or explicitly stopped. Completion also disarms: receiving RR73/73 logs the QSO, and the answerer still sends one courtesy 73, repeated only when the partner repeats its final message. It never automatically chooses the next station. Unsupported message branches fail closed. Selecting a station does not arm or transmit; Reply/CQ is explicit.

The optional automatic CQ loop rides on these rules without weakening them. It starts only through the lease-gated CQ path, and every transmission still crosses the §15.2 chain immediately before PTT. A completed QSO re-arms CQ and resets the loop's idle timer; retry exhaustion or partner loss re-arms without resetting it; manual or fault disarm stops the loop and is never re-armed automatically. Lease loss and the configurable idle timeout (60–3600 s, default 600 s) stop the loop, and TX off/STOP terminate it through the existing paths; every transition is audited.

Transmissions are pumped by a slot-parity driver gated on the sequencer's per-QSO `tx_phase` (UC-003 opposite slot); it pulls at most one sequencer message per eligible slot, encodes it through the supervised Worker and hands the waveform to the safety controller. The decision is provisional: while the sequencer is idle at slot start, the driver keeps the slot's TX window open until `TX_DECISION_CUTOFF_SECONDS` (5.0), polling so a manual Reply transmits as soon as it is armed; a fit guard refuses any start past ~2.4 s into the slot (a 12.64 s waveform must fit a 15 s slot), deferring to the next eligible slot so no transmission overruns the slot boundary or deafens the partner's next transmission. The driver never retries and never touches PTT itself; every failure is counted, encode failures are reported to the §15.5 fault matrix, and safety refusals are absorbed without faulting.

## 15.7 Verification

Automated tests cover lease loss, observer STOP, duplicate STOP, late slot, retry exhaustion, each interlock, worker crash and restart invariants. macOS real-radio acceptance observes actual PTT release for disconnect, CAT/audio failure and manual STOP scenarios.
