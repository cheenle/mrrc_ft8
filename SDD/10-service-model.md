# 10. Service Model

## 10.1 HTTP Services

| Area | Representative operations |
|---|---|
| Session | login, logout, re-authenticate, current session |
| Lease | acquire, heartbeat, release, inspect |
| State | authoritative snapshot, health, capabilities |
| Operation | select station, Reply, CQ, Enable TX off, STOP TX, clear fault |
| Radio | set band/mode/offset when safe |
| Settings | read/update supported categories with validation |
| Logs | list QSO, audited void/undo, ADIF export |
| Diagnostics | health summary, password-gated raw bundle export |

Exact paths are versioned under `/api/v1`. Mutating commands carry an idempotency key and expected state revision. A rejection returns a stable reason such as `lease_required`, `stale_revision`, `unsafe_slot`, `interlock_open` or `unsupported_message`. The CQ operation accepts an optional `{"loop": true}` body that starts the automatic CQ loop (§15.6) through the same lease-gated arm path; the plain form arms a single CQ as before. The clear-fault operation is the lease-gated recovery channel for latched interlocks (§15.5): an empty body clears every latched fault, `{"interlock": "<name>"}` clears only that one, an unknown name is rejected with 422, and clearing never re-arms — re-arming stays with the operator's CQ/Reply path.

## 10.2 WebSocket Services

| Stream | Payload | Backpressure |
|---|---|---|
| `/ws/v1/state` | snapshot/delta, lease, slot, TX/PTT, health, sequencer | Preserve latest authoritative state; close irrecoverably slow client |
| `/ws/v1/decodes` | slot decode batch and candidate updates | Bounded ordered queue; reconnect can fetch history |
| `/ws/v1/waterfall` | compact binary spectrum frame | Drop oldest frames, keep newest |

All upgrades authenticate by secure cookie and validate Origin/Host. No query token is accepted.

Decode batch entries carry `dt`/`freq`/`to_me` alongside slot, UTC, SNR and text so clients can render band-activity rows without re-parsing message bodies. The state snapshot's sequencer view includes `cq_loop` (active flag and idle-timeout countdown) when the loop controller is wired.

Every state frame (the hello snapshot and each broadcast) is decorated per connection with the subscribing session's own lease view: `lease.mine` is recomputed against that session, never copied from the shared snapshot, so the holder's control UI and the observers' views are always correct. The snapshot's `selected` is a `{"call", "grid"}` object (or null); REST mutation responses keep returning the callsign as a plain string. Clients render the `cq_loop` countdown with a local 1 Hz tick between snapshots and resynchronize whenever a new snapshot arrives.

## 10.3 Control Rules

- Observe/read operations require authentication.
- TX-starting, band/mode/offset and sequencer mutations require the lease.
- STOP requires authentication but explicitly bypasses the lease.
- Diagnostic export requires recent password re-authentication but not the lease.
- The server validates authority again immediately before PTT.

## 10.4 Worker Protocol

Protocol v1 is a synchronous local request/response control protocol over one
duplex multiprocessing pipe. Every frame is a UTF-8 JSON object no larger than
65,536 bytes with exact keys and nonnegative int64 `generation` and `request_id`.
Serialization sorts keys, uses compact separators and forbids NaN/Infinity;
parsing rejects duplicate keys, non-objects, booleans used as integers, missing
or extra fields, invalid UTF-8/JSON and unknown versions/types. The exact types
are `ping`, `pong`, `decode`, `decode_ok`, `encode`, `encode_ok`, `error`,
`shutdown` and `stopped`. ABI/capability negotiation remains inside the Worker
binding at construction and is not duplicated in the wire schema.

The control pipe carries no audio or waveform payload. `decode` names an exact
logical `<i2`, `[180000]`, 360,000-byte parent-owned segment plus complete
DecodeConfig and deadline metadata. `encode` names an exact logical `<f4`,
`[606720]`, 2,426,880-byte parent-owned segment plus message/frequency/48 kHz
metadata. The Worker validates generation before any open, validates the
observable allocation size, maps only the fixed logical shape, closes but never
unlinks, and returns immutable result/encode metadata. Darwin rounds named POSIX
shared-memory allocations to `mmap.PAGESIZE`; there the one accepted observable
size is exactly `ceil(logical_nbytes / mmap.PAGESIZE) * mmap.PAGESIZE`, while the
descriptor and NumPy view retain the logical byte count. Other supported hosts
require the observable size to equal logical `nbytes` exactly.

`decode_ok` is capped at 256 exact nine-field results. Application, ABI and
shared-memory failures return a correlated stable-code/printable-detail `error`
without traceback, path or exception representation and the Worker remains
available. Generation mismatch is an application error and never opens the
named segment. Malformed/oversize protocol or pipe corruption terminates the
Worker nonzero so supervision can fail closed. The synchronous Worker loop
executes one DSP operation at a time; timeout/restart and stale-response policy
belong to the supervisor.

## 10.5 Configuration Service

Settings are grouped as Operating, Decoder, Waterfall, Filters & Highlighting, Station & Radio, Logs & Diagnostics, and Help/About. Updates are schema validated. Safety-impacting changes are rejected during TX and may require monitor-mode restart. The schema includes `cq_loop_idle_timeout_s` (integer 60–3600 s, default 600), the automatic CQ loop's idle stop.

## 10.6 Audit Events

Audit records include actor session, timestamp, remote address, operation, target, prior/new state revision and result. At minimum: login failures, lease changes, CQ/Reply, Enable TX, STOP, faults, QSO void and diagnostic export.
