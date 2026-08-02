# Test Inventory

The runtime test suite includes guardian regressions and hardware-free DSP ABI coverage. The standard FT8 encode tests perform their own fresh configure/build and verify the exact 48 kHz/606,720-sample waveform, sent text, deterministic zeroed failure outputs, error statuses, and NULL-pointer safety. Standard decode tests also use a fresh shared library, synthesize `CQ K1ABC FN42`, resample it to an exact 12 kHz/180,000-sample int16 slot, and verify the fixed Fortran batch, slot/frequency identity, ABI layouts, rate/shape/capacity rejection, both struct-size boundaries, every nullable pointer, deterministic count/overflow clearing, and no partial result copy on failure. Native configuration regressions require QSO progress 0–5, sensitivity 1–3, a valid `HHMMSS`, and a 100–4910 Hz increasing window at least 100 Hz wide; invalid values fail before sample/result mapping or vendor entry. A same-process CQ→CQ→empty→CQ sequence verifies that slot identity, batch reset, stage-41 reset and vendor saved state do not leak across long-lived Worker requests.

Improved regressions use the same real ABI/fixture for profiles 0–4, enforce profile/thread/cycle bounds and all shared fail-closed cases, verify exact cross-pass deduplication, and exercise a 12-thread 100 Hz window. Exhaustive native partition tests cover every legal band/thread combination, while the team-limit subprocess proves a smaller actual OpenMP team returns `WSJT_E_INTERNAL=8` with no partial batch copy. Multisignal and repeated 4-thread/3-cycle tests compare normalized sets without imposing result order, covering band-local subtraction plus thread-private slot, downsample, FFTW, OSD and cycle state. Improved request-state regressions structurally verify all six even/odd CQ, MyCall and QSO histories receive the `6000.0` frequency sentinel plus zero offsets/symbols and that AP masks/A8 eligibility are rebuilt from each request's AP/DX context; same-CDLL CQ→empty→CQ and context-switch sequences reject observable leakage.

The native A8 probe checks the strict 3 Hz atomic threshold, a clean direct-A8 fixture decodes three times, and an ordinary near-Rx decode produces no observable AP type 8 result. The fixed weak direct-A8 fixture is `xfail(strict=True)` because it is not reproducible in a fresh library process; it documents a remaining evidence limitation rather than claiming weak-signal sensitivity. The fixture sets the upstream `OMP_STACKSIZE=10M` contract before NumPy/SciPy or the native/OpenMP runtime can load. Set `MRRC_FT8_DSP_BUILD_TYPE=Debug` to run the fresh fixture with Fortran bounds checks.

The dynamic-export regression permits exactly the four implemented ABI functions and rejects internal batch/callback leakage. A portable CMake rendering test verifies the Linux production version map contains those four exports and the test-hook map adds only `wsjt_test_ft8_a8d`, with no unexpanded placeholder. Vendor policy applies exact forward and inverse transformations for all seven registered Improved copies and requires byte identity with vendor after reversal, while the independent full-tree digest remains unchanged.

The opt-in I9 benchmark (`MRRC_FT8_I9_BENCHMARK=1`, JSON artifact via `MRRC_FT8_I9_JSON`) fresh-builds the library, drives the real supervised Worker through `WorkerSupervisor` and times production decode requests for profiles 0–4 across 1–12 threads plus a cycles-3 spot check on one deterministic five-signal noisy slot. It asserts every configuration recovers at least three of the five known CQs and that the default profile-3/Auto-thread configuration meets the provisional 2.5 s TX decision cutoff; an always-on unit test pins the Auto clamp policy. Per-CPU results are transcribed into SDD §13.4.

The Python core-boundary suite verifies frozen/slotted request and result models,
exact C struct field types/sizes/offsets and all four complete function
signatures. Both decode signatures are compared with a test-owned literal
seven-item ABI list, and a mutation probe proves that substituting `c_int32`
for the `c_int64` slot ID is rejected. The suite also covers
construction-time ABI/capability/reserved negotiation, aligned
12 kHz int16 decode and aligned/writeable 48 kHz float32 encode contracts,
every configuration/ASCII bound, status-to-exception mapping, safe result-text
copying, caller-owned waveform metadata and one-call path dispatch. Poisoned
production-adapter probes prove failed calls never consume native output fields;
successful output metadata and the common production/test adapter boundary both
enforce the 256-result and binary-overflow contracts before any record copy.
A four-thread fake-native probe proves the single module-level `DSP_LOCK`
serializes calls. For the explicitly enumerated static forms, AST dependency
tests enforce that only `binding.py` may load a shared library, only
`worker.py` may import the binding, and both allowlists use exact
`server/core/` paths. Synthetic modules cover imported ctypes aliases,
`CDLL`/`PyDLL`/`WinDLL`/`OleDLL`, the four standard `LoadLibrary` objects,
attribute-assignment aliases, `from ctypes import *`, imported NumPy aliases
using `.ctypeslib.load_library`, and direct `numpy.ctypeslib.load_library`
aliases. This is finite syntactic coverage, not general data-flow or dynamic-
import analysis. The required `OMP_STACKSIZE=10M` default precedes NumPy import
and native loading.

Protocol and Worker tests cover every Protocol v1 frame type with deterministic
bounded UTF-8 JSON, exact keys, strict scalar/container types, duplicate-key and
NaN/Infinity rejection, fixed RX/TX shared-memory descriptors, all DecodeConfig
fields, the nine-field/256-record decode result bound, and sanitized errors.
Real `spawn` tests build an isolated fresh library, observe no unsolicited ready
frame, exercise ping/shutdown, write a caller-owned 48 kHz TX segment, resample
that waveform into a known `CQ K1ABC FN42` 12 kHz slot, and decode it through the
long-lived Worker. Generation mismatch is rejected before opening a deliberately
missing segment; observable under/oversize segments and binding validation errors
produce matching sanitized responses while the loop remains usable. The parent
can still unlink every segment, while malformed/oversize pipe frames make the
Worker exit nonzero. AST coverage proves `worker.py` imports neither NumPy nor
the binding at module load and sets `OMP_STACKSIZE=10M` before both local imports.
On Darwin, POSIX shared-memory size is observably rounded to `mmap.PAGESIZE`, so
tests require exactly that one rounded allocation and use cross-page sizes for
under/oversize rejection; the protocol descriptor retains exact logical bytes.

Supervisor tests run scripted spawn-level fake Workers (sentinel slot IDs, no
shared memory or DSP library) through the real Protocol v1 boundary. Coverage
includes ping-verified startup and READY health, monotonic request IDs,
returned decode/error frames without restart, outgoing-frame and request-type
validation that never touches the Worker, fail-closed IPC timeout, mid-request
and pre-request crash detection, stale-generation, corrupt and unexpected-type
responses — each triggering exactly one bounded restart with a fresh generation
— plus bounded crash-loop degradation to DEGRADED, graceful and forced stop,
idempotent/double-start lifecycle rules, `not_running` after stop and
failure-carrying transition callbacks.

Engine message/sequencer tests are hardware-free. The parser suite covers CQ
(with FD/DX modifiers and invalid-callsign rejection), directed grid, report
and R+report shapes, all three end-of-message tokens including their regex
collisions with grid/report shapes, hash callsigns, free text, whitespace
normalization, base-callsign stripping and CQ-aware addressing. The sequencer
suite drives both complete QSO flows (CQ side and answerer), the NFR-055
one-plus-three retransmission budget and its partner-progress reset, retry
exhaustion with retained context, anti-QRM partner-loss auto-stop,
third-station/free-text rejection, the RRR and RR73-shortcut branches,
courtesy-73 repetition rules, protocol-range report clamping, terminal DONE
behavior and log-record metadata/popping.

The remaining TX-path engine suites are hardware-free too. The production-
encoder suite drives `SupervisorEncoder` through a fake supervisor validated
against the real Protocol v1 schema: the exact encode-frame contract and
waveform copy out of one reused TX segment, segment reuse across requests,
sanitized error and unexpected-type frames mapping to `TxEncodeError`, and
idempotent close. The TX-driver suite pins slot-parity gating (even slots by
default), one sequencer message per eligible slot, idle-sequencer silence,
broad failure counting (`tx_attempts`/`tx_failed`) without propagation for
encode, TX-refused and WorkerFault paths, the rule that a safety `TxRefused`
never reaches the error hook (a STOP-cancelled playback must not latch the
DSP interlock), the in-flight overlap guard, the
audit hook, retry-exhaustion silence and invalid-parity rejection. The
CQ-loop suite covers DONE re-arm with idle-timer reset, retry-exhaustion and
partner-loss re-arm without reset, manual/fault disarm and lease loss
stopping the loop, the idle timeout stopping and disarming, idempotent
audited start, arm-refusal failure and the snapshot status shape. The
QSO-log suite proves a completed QSO is recorded exactly once into the
canonical store and an idle sequencer records nothing.

Orchestrator tests drive the UTC slot loop with an injected fake clock and
instant sleeper: `floor(epoch/TRperiod)` identity, starts and parity for FT8
and FT4 periods, per-boundary decode dispatch order with exact-epoch dispatch
timestamps, on-time messages fed to a real sequencer with their decode SNR,
late batches marked display-only and withheld from the sequencer (deadline-miss
counter), slot-mismatch and decoder-exception error events with loop
continuation, and skipped or wrongly-sized slot sources. Dispatch timing is
pinned to slot boundary + delivery grace (real audio lands ~one block late;
a boundary-exact read skipped every slot in the foreground/launchd
deployment). The composition config suite now also pins §12.6 parsing: audio
device by index or name, decoder profile 0–4 and threads `auto`/1–12 with
fail-startup validation.

Receive-path tests stay hardware-free. The converter suite proves sample-exact
block-boundary independence against a one-shot reference, passband tone
fidelity, >53 dB anti-alias stopband rejection and int16 clipping. The UTC
ring suite covers exact contiguous slot reads, gap-range invalidation of only
the missing span, eviction with late-write drop accounting and duplicate-write
tolerance. The capture seam is driven through a fake stream factory verifying
the 48 kHz/mono/int16 capture contract with in-seam float32 normalization
(the FT-710 UAC float32 path intermittently delivers toneless noise while
int16 always delivers the band — 2026-08-03 field finding), block-to-ring
epoch wiring anchored to the CoreAudio ADC hardware timestamp (a delivery
stall becomes one recorded gap, never a permanent time shift of every
later slot), overflow
counting, idempotent stop, and stop→start stream recreation (the degraded-
session bounce). The capture-health monitor is pinned for cold-band silence,
healthy decodes, the once-per-episode hot-but-silent streak edge, recovery
reset and the threshold boundary. The production-decoder suite runs a fake
supervisor that validates every frame against the real Protocol v1 schema,
checking the fixed descriptor, slot→`utc_hhmmss` mapping, plain-str path,
segment byte fidelity and reuse, `DecodeBatch` conversion, sanitized error
mapping, timeout forwarding and unlink-on-close.

The rig suite drives the rigctld client against a real local TCP fake
(`asyncio.start_server`) that can inject silent timeouts, garbage replies,
mid-session drops and `RPRT -n` errors. It covers frequency/mode/PTT
round-trips, error-code propagation, input validation before any I/O,
command serialization through the lock, transparent reconnect after a broken
session, fail-closed behavior when rigctld is unreachable and idempotent
close. The TX playback suite proves block-exact writes through a fake output
stream, the 48 kHz/mono/float32 stream contract, cancellation of a blocked
write, device-loss mapping, concurrent-play rejection and invalid-buffer
rejection before any stream opens. The safety suite drives the central PTT
authority through the §15.5 fault matrix with a fake rig and controllable
watchdog sleeper: monitor-only startup with PTT-off, keying order around the
audio, unarmed/faulted refusal, idempotent mid-play STOP that disarms a real
sequencer, PTT-on/PTT-off failure with faulting and reconnect retry, audio
device loss, per-transmission and aggregate watchdog trips, and buffer
rejection before PTT is ever keyed, and a write failure after `cancel()`
counting as normal cancellation (real PortAudio fails the aborted write,
e.g. PaErrorCode -9986). The capture seam adds two acceptance-driven
regressions: ±ms per-block clock jitter must not carve ring gaps
(sample-count-anchored epochs), and a >250 ms stall re-anchors and is
recorded as exactly one gap. The waterfall suite proves tone peak
placement, silence floor quantization, block-size-independent cadence and
epochs, UTC-gap line reset, `WF01` binary round-trip with corruption
rejection, and the lossy fan-out: bounded queues drop the oldest frames,
keep the newest, count drops and never block publishing (SC8). The
persistence suites run against real SQLite (in-memory and file): migration
and reopen preservation, full-field QSO round trips, the audited 30-second
void window with trail and audit evidence, `ABORTED_RESTART` transitions,
retention that expires only decode/audit rows, JSON settings and table-name
allow-listing. The ADIF suite verifies exact field lengths, epoch-derived
dates, non-void-only export, optional-field omission and non-printable
rejection. The auth suite covers Argon2id salt/format and constant-time
verification, opaque session issue, idle refresh versus 12 h absolute
expiry, logout/sweep, the progressive bounded login delay and its reset on
success, the five-minute re-authentication window and the Host/Origin
validation matrix. The lease suite covers single-holder arbitration,
owner-only heartbeat renewal, one-shot dead-man firing on TTL expiry and on
immediate controller disconnect, release/takeover semantics, audited lease
events and no lease restoration across restarts. The REST suite drives the
full `/api/v1` surface through FastAPI's TestClient: hardened-cookie login
and audited failures, Host/Origin rejection, lease arbitration between two
sessions, stale-revision and idempotent-replay semantics, select/Reply/CQ
arming with audit, CQ-loop start (lease-gated, `{"loop": true}` snapshot
exposure with the legacy single-CQ path preserved) and its idle-timeout
setting bounds, observer STOP without a lease, band rules
(invalid/TX-active/rig-down), QSO listing with audited void, ADIF export,
re-auth-gated diagnostic bundles free of secrets, and settings schema plus
the TX lock on safety-impacting keys. The WebSocket suites cover state
coalescing with slow-client close, ordered decode overflow semantics and
reconnect history, cookie/Origin rejection at upgrade, lease drop on
controller disconnect and binary waterfall delivery. Static contract tests
pin the no-inline-JS rule, the landscape manifest, service-worker cache
exclusions for API/WS, asset existence, the `[hidden] { display: none }`
guard that keeps the login overlay/cockpit toggling real (without it the
waterfall canvas paints over the login form and the password field is
untypable), and the floored canvas-resize comparison (a raw fractional-rect
comparison clears the bitmap every frame, so the waterfall never
accumulates). The decode feed is pinned as a chronological
scroll (every message a row, slot time on the row) with per-slot
separators carrying slot UTC and the polled dial frequency, and with
slot replacement so reconnect replay never duplicates rows. Static contract tests also pin the Band Activity row columns
(UTC/SNR/dt/freq/text), the CQ-loop countdown in the safety bar and the
loop flag on the CQ intent. The candidate-tap contract pins that a tap on a
decode row never fails silently: a free control lease is taken implicitly
(WSJT-X-style single tap) and every rejection surfaces through the toast,
whose element/styles/module are pinned into the app shell; the login form is
pinned to carry a visually-hidden username field for password managers. A
stale-display contract pins that dead streams cannot impersonate a live
band: the waterfall canvas dims while its WS is offline, candidate rows age
into a stale class on a slow tick, and row time derives from the slot
itself so replayed history never re-floats to the top. The composition suite runs the
real lifespan through TestClient: monitor-only startup with PTT-off,
`ABORTED_RESTART` marking of seeded interrupted QSOs, dead-man wiring from
lease disconnect to priority STOP, STOP-first shutdown and static shell
serving, TX-driver and CQ-loop wiring, watchdog-polled recording of a
completed QSO, decode message views carrying dt/freq/to_me, plus a live
smoke check of `python -m server.main`. The deploy
suite pins the §12 artifacts textually — Caddyfile loopback proxy target,
unprivileged systemd user with audio groups, `OMP_STACKSIZE=10M` in both
service managers and no committed secrets — and round-trips the real
`--hash-password` bootstrap CLI through a subprocess into `PasswordHasher`,
plus the root Caddy daemon plist (runs `caddy run` against
`/etc/caddy/Caddyfile` with auto-restart).
The latency suite pins the NFR-002 histogram bucket boundaries,
per-(profile, threads) snapshot shape, negative-input rejection, the
`SupervisorDecoder` recording path and the `/health` exposure of
`decode_latency` alongside the deadline-miss counter.
`acceptance/real_radio.py` is the hardware-gated FT-710 runner (not part
of the pytest run): preflight, tuned live-slot supervised decode and a
`--tx`-gated PTT/STOP phase; its first runs drove the two RX/TX fixes
above and measured the §13.5 I10 PTT timings.

Later implementation milestones will add coverage for:

- FT4 and remaining synthetic DSP regressions.
- Linux synthetic audio and mocked rigctld service acceptance.

Real FT-710 tests are an explicit macOS release checklist and are not part of the hardware-free pytest run.
