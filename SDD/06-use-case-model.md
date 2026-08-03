# 6. Use Case Model (ART 0508)

## 6.1 Core Use Cases

### UC-001 Authenticate and Observe

Operator opens the HTTPS domain, authenticates with the strong password, receives a secure session cookie, and connects state/decode/waterfall streams. CAT/audio/DSP and lease state are visible. Failure returns no radio data or controls.

### UC-002 Acquire Control Lease

An authenticated session requests the lease. If free, the server grants it and expects a heartbeat every 5 s; TTL is 15 s. Other sessions remain observers. Restart never restores the lease.

### UC-003 Select a Station and Reply

The operator taps a candidate to inspect it, then separately taps Reply. The server verifies lease, health and supported message semantics, sets call/grid, RX offset and opposite TX slot, then prepares the single-QSO sequencer. Selection alone never transmits. If the tapping session holds no lease and the lease is free, the client first acquires it implicitly through the standard grant path (UC-002 invariants unchanged); a tap rejected because another session holds the lease, or for any other reason, produces visible feedback rather than failing silently.

*Implemented (2026-08-03):* the "opposite TX slot" is carried by the sequencer's `tx_phase` — `1 − slot_id % 2` of the slot the partner's message was decoded from, plumbed through `operation/select`'s `slot_id` field from the UI candidate — and the `TxDriver` gates on it, so a reply to an even-slot caller transmits on odd slots and vice versa. The reply decision is provisional: when the sequencer is idle at slot start, the driver polls until `TX_DECISION_CUTOFF_SECONDS` (5.0) so a Reply transmits as soon as it is armed, with a fit guard refusing any start past ~2.4 s (the latest a fixed 12.64 s waveform fits a 15 s slot); a Reply armed past that deadline defers to the next eligible slot.

### UC-011 Select FT8 Band

The lease holder picks a band (7/14/21/28 MHz) from the top-bar selector. The server tunes the rig dial to the band's FT8 frequency (7.074/14.074/21.074/28.074 MHz) through the lease-gated `/radio/band` mutation. A free lease is acquired implicitly (UC-002); the change is rejected while TX is armed, and failures are surfaced rather than silent. The selector reflects the rig's polled dial frequency when it lands on a known band.

### UC-004 Call CQ

The lease holder explicitly starts CQ. The sequencer generates the standard CQ message and sends only in an eligible slot. It may proceed with one caller; it does not select a new target after completion.

### UC-005 Complete One QSO

The sequencer advances through report, roger-report, rogers and signoff using Tx1–Tx6 semantics. Each message has one initial send plus at most three retransmissions. RR73/73 logs the QSO and disarms TX. Retry exhaustion also disarms and retains context.

### UC-006 Emergency STOP TX

Any authenticated session can invoke STOP without the lease. The server cancels audio and queued TX, requests PTT off, disarms the sequencer and records actor/reason. The operation is idempotent and prioritized over normal traffic.

### UC-007 Handle Fault or Disconnect

CAT/audio/DSP/clock failure, controller disconnect or lease expiry during TX invokes the same immediate safe-stop path. Recovery restores monitoring and context only; the operator must reacquire control and re-arm.

### UC-008 Restart Service

Startup requests PTT off, creates no lease, disarms TX, marks an interrupted QSO `ABORTED_RESTART`, restores settings/logs and enters monitor mode.

### UC-009 Review and Export Logs

Authenticated sessions browse QSO history and export ADIF. Automatic completion can be undone for 30 seconds; undo creates an audited void and updates generated export data.

### UC-010 Export Diagnostics

An authenticated session re-enters the password, acknowledges that the archive is raw and sensitive, and exports local config/log/audit/health data. A control lease is not required. Secret hashes and cookie values are excluded.
