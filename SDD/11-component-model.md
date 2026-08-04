# 11. Component Model

## 11.1 DSP Layer

| Component | Responsibility |
|---|---|
| `dsp/CMakeLists.txt` | Select vendor/patched sources and build shared library |
| `dsp/*_shim.f90` | Stable `bind(C)` ABI, strict rate/shape validation and vendor decode dispatch |
| `dsp/wsjt_batch.f90` | Fixed 256-result Fortran-owned batch; OpenMP callbacks append without crossing into Python/C |
| `dsp/wsjt_partition.f90` | Exact inclusive integer-frequency partitioning for 1–12 OpenMP bands |
| `dsp/wsjt_a8_gate.f90` | Atomic near-Rx gate used by ordinary decode callbacks and the A8 owner |
| `dsp/wsjt_test_hooks.f90` | Optional non-production direct-A8 probe, compiled only with `MRRC_FT8_TEST_HOOKS=ON` |
| `dsp/ft8_stdcall.f90` | Headless extraction of the standard-callsign helper without the unrelated Q65 encoder dependency |
| `dsp/patched/` | Explicit copies required for build adaptation; never silent vendor edits |
| `server/core/models.py` | Frozen/slotted decode configuration, batch/result and encode metadata value types |
| `server/core/binding.py` | ctypes types, ABI validation, global lock, rate/shape assertions |
| `server/core/protocol.py` | Exact bounded Protocol v1 JSON schema and fixed shared-memory descriptors |
| `server/core/worker.py` | Spawn-only binding owner, shared-memory mapping and synchronous request dispatch |
| `server/core/supervisor.py` | Spawn, health, timeout, restart generation and failure reporting |

The standard FT8 ABI accepts exactly 180,000 mono int16 samples at 12 kHz.
It also fails closed unless QSO progress is 0–5, sensitivity is 1–3, UTC is a
valid `HHMMSS`, and the decode band satisfies
`100 <= low < high <= 4910` with at least 100 Hz width. These native checks run
before sample/result mapping and vendor entry; failed calls clear count and
overflow without copying result records.
One ABI request resets one result batch, then reproduces the vendor full-slot
disk orchestration on a single `ft8_decoder`: staged `nzhsym` calls 41, 47 and
50 expose 141,696, 162,432 and 172,800 caller samples respectively and zero
the remainder. This is required because the standard depth-2/3 decoder loads
its saved working buffer during the early stages; the third stage alone does
not load fresh input. Results cross the C boundary only after all three stages
return. The shared library exports only the ABI entry points; its callback and
batch module symbols remain internal.
Same-process CQ→CQ→empty→CQ regression covers batch reset, stage-41 vendor
reset, slot replacement and saved-state isolation expected by a long-lived
Worker.

The Improved ABI applies the same pointer, struct, 12 kHz/180,000-sample,
capacity and native configuration checks, plus profile 0–4, thread count 1–12
and cycle count 1–3 validation. Profiles 0–4 dispatch `41+49`, `41+46+50`,
`48`, `49` and `50` respectively. Improved pass `N` exposes
`min(180000, N*3456)` samples and zeroes the tail. The configured inclusive
frequency window is divided into exactly the requested number of adjacent,
non-overlapping bands; a 100 Hz configured width is 101 integer frequency
points, so even 12 threads receive non-empty 8- or 9-point bands. All OpenMP
callbacks append to the Fortran batch. After every profile pass returns, stable
deduplication keeps the first exact `(text, rounded Hz, rounded 0.1 s)` key;
overflow remains asserted if the pre-dedup fixed batch ever exceeded 256.
Before every Improved request, the Worker-side initializer clears all even/odd
CQ, MyCall and QSO detection histories across the vendor thread arrays:
frequency is the vendor-compatible `6000.0` end sentinel, time offset is zero
and every complex symbol is zero. A8 eligibility is also assigned afresh from
the request: AP must be enabled, DX call length must be at least three and the
four-character DX grid prefix must be complete. Thus neither detection history
nor A8 state can carry across profiles or requests in a long-lived Worker.

The OpenMP region must contain exactly the requested team size. A constrained
runtime that forms a smaller team returns `WSJT_E_INTERNAL=8`; the C shim leaves
count/overflow cleared and does not copy the partial Fortran batch. Every team
member uses `copyin(dd8)` with thread-private slot, downsample and FFTW state,
and cycle 2/3 scratch belongs to its private decoder instance. Consequently
successful subtraction changes only that thread's assigned frequency band.
This band-local subtraction intentionally differs from the upstream GUI's
unsynchronized shared `dd8` subtraction and is the accepted deterministic
headless behavior. The thread-private FFTW plan caches persist for the Worker
process lifetime and are reclaimed at process exit.

A8 has one deterministic owner: the band containing Rx, or band 1 when Rx is
outside the configured window. Ordinary callbacks atomically clear the shared
gate for a result strictly within 3 Hz of Rx; after the decode barrier the owner
atomically snapshots that gate before any direct A8 attempt. Result collection
is safe but OpenMP scheduling determines append order, so result order is
unspecified. The ABI does not sort it.

Improved `sync8var` uses multi-megabyte automatic arrays on every OpenMP thread.
The DSP process therefore has a runtime prerequisite, inherited from the
upstream GUI launcher: `OMP_STACKSIZE=10M` must be present before NumPy/SciPy,
the shared library, or any OpenMP runtime can load. The Task 5 direct-ABI fixture
sets it at module import. `worker.py` itself imports neither NumPy nor the binding
at module load. The first executable statement in `worker_main` sets the same
default, then locally imports NumPy and `CoreBinding`; the binding repeats the
pre-load default defensively before its own NumPy import. Constructing the one
binding loads either the explicit library path or the platform-specific
`dsp/build/libwsjt_core` default and sends no unsolicited ready message.

`protocol.py` applies one exact schema on both encode and decode. A 64 KiB frame
limit is checked before parsing and after serialization; nine frame types,
complete DecodeConfig metadata, fixed RX/TX descriptors, 256 exact result
objects and sanitized errors are the entire v1 control surface. The Worker
checks generation before `SharedMemory(create=False)`, verifies the uniquely
observable allocation size, maps the fixed logical NumPy shape, makes decode
views read-only, and closes in `finally` without unlinking. Darwin's POSIX SHM
allocation is uniquely page-rounded and must equal the logical size rounded up
to `mmap.PAGESIZE`; other platforms require exact logical size. Only the parent
owns unlink. Valid request/ABI/SHM failures are sanitized and recoverable;
protocol/pipe corruption or unexpected internal corruption exits nonzero.

`CoreBinding` configures signatures for exactly the four production exports and
queries `wsjt_get_abi_info` at construction. ABI version, ABI-info/result
structure sizes, capacity, both RX/TX domains and the complete profile/thread/
cycle capability set plus zero reserved field must match before any encode or
decode may run. Python validates array shape/contiguity/type/alignment, native
configuration ranges and bounded ASCII fields before acquiring the one
module-level reentrant lock; TX output must also be writeable. Inside that lock
it calls exactly one selected decode entry point or one encode entry point.
A failed decode status discards every native out value without reading or
copying it. On success, count must be 0–256 and overflow exactly 0/1; the common
adapter boundary repeats those metadata checks before copying records into
immutable Python values. Encode writes only to the caller-owned exact
606,720-sample float32 buffer and returns immutable metadata; no raw CDLL handle
crosses the private adapter.

Production builds export exactly the four C ABI functions. ELF builds generate
the linker version map from a configured template; test-hook builds add only
`wsjt_test_ft8_a8d`. The hook validates direct-A8 plumbing with a clean fixed
signal and is never part of the production surface.

## 11.2 Engine Layer

| Component | Responsibility |
|---|---|
| `orchestrator.py` | UTC slot identity, capture/decision deadlines, mode/profile scheduling |
| `dsp_decode.py` | Supervisor-backed SlotDecoder: exact slot → shared memory → Protocol v1 batch |
| `dsp_encode.py` | Supervisor-backed encoder: message → shared memory → Protocol v1 encode |
| `tx_driver.py` | Slot-parity TX pump gated on the sequencer's per-QSO `tx_phase`; provisional (I9) decision window (polling, 5 s cutoff) with a fit guard deferring past ~2.4 s — one sequencer message per eligible slot, encode → gated transmit |
| `cq_loop.py` | Automatic CQ loop: DONE/retry/partner-loss re-arm, lease/idle/manual/fault stop |
| `qso_log.py` | Sequencer log record → canonical QSO store offload |
| `audio_rx.py` | 48 kHz capture, one 4:1 conversion, UTC ring (absolute-index `X % capacity` keying; eviction never shifts data) and overrun metrics |
| `capture_proc.py` | Isolated capture subprocess + parent supervisor (fresh-session restart on stall/death) |
| `audio_tx.py` | Bounded 48 kHz playback with cancellation |
| `waterfall.py` | Spectrum frames (3 kHz FT8 passband only) and lossy fan-out input |
| `rig.py` | Async rigctld TCP client only |
| `safety.py` | Interlocks, PTT watchdog, idempotent priority STOP |
| `sequencer.py` | One-QSO transitions, Tx1–Tx6, retry and terminal behavior; carries the per-QSO TX phase (UC-003 opposite slot) |
| `msgparse.py` | Supported standard-message parsing and validation |
| `repository.py` | SQLite transactions, retention and schema migrations |
| `adif.py` | ADIF generation/export from canonical QSO data |
| `adif_import.py` | JTDX ADIF export parser + idempotent incremental import (`sync_jtdx_log`); dedupe key `(dx_call, utc date, started_utc, band)`; `source='jtdx'` rows |
| `dxcc.py` | cty.dat parser + callsign→DXCC lookup + full-scan summary (total / entities / by_band) |
| `main.py` auto-call | `is_new_dxcc` decode marking + `auto_call_candidate` decision + `_auto_call` (safety-armed sequencer reply, audit) |

## 11.3 Web Layer

| Component | Responsibility |
|---|---|
| `main.py` | Lifespan composition root — wires the TX driver, CQ loop controller, watchdog-polled QSO logging, startup + hourly JTDX ADIF sync — and Uvicorn entry point |
| `web/auth.py` | Password hash, sessions, re-auth and rate limiting |
| `web/lease.py` | Acquire/heartbeat/release/expiry and dead-man callback |
| `web/api.py` | Versioned REST intent endpoints |
| `web/ws.py` | Separate state/decode/waterfall streams and bounded queues |
| `web/static/` | Landscape PWA modules (candidates, waterfall, safety bar, `band.js` band selector), CSS, manifest and service worker |

The lifespan's background maintenance loop (session sweep plus retention) catches and logs each tick's exceptions, so one failed sweep cannot kill the loop; the 1 s lease-expiry watchdog is guarded the same way.

## 11.4 Dependency Rules

- Web depends on engine interfaces, never on ctypes.
- Engine requests DSP through the supervisor, never imports the shared library.
- `server/core/worker.py` alone imports binding;
  `server/core/binding.py` alone may use standard ctypes DLL loaders. Static
  dependency regressions enforce these exact paths, including function-local
  aliases and `CDLL`/`PyDLL`/`WinDLL`/`OleDLL`/`LoadLibrary` forms.
- Only `rig.py` speaks rigctld; no module opens serial.
- Only `safety.py` controls PTT and audio cancellation.
- Static clients submit intent and cannot construct raw PTT actions.

## 11.5 Vendor Patch Register

Each file under `dsp/patched/` records original vendor path, upstream revision,
minimal difference, reason and regression evidence here and in `AGENTS.md`.

| Local copy | Origin / upstream revision | Exact difference | Reason | Regression evidence |
|---|---|---|---|---|
| `dsp/patched/encode174_91var.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/encode174_91var.f90` | One absolute LDPC-generator include becomes `include 'ldpc_174_91_c_generator.f90'` | Relocatable headless build | Per-file one-replacement inverse transform is byte-identical to vendor; all five synthetic profiles decode |
| `dsp/patched/osd174_91var.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/osd174_91var.f90` | Relative LDPC include; move the complete `first_osd` check inside its named critical section | Relocatable build and single concurrent generator-matrix initialization | Exact reversible transformations plus parallel profile stress |
| `dsp/patched/four2avar.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/four2avar.f90` | Relative FFTW include; make the saved plan registry `THREADPRIVATE` | Relocatable FFTW and per-thread plan/address cache | Exact reversible transformations plus repeated parallel-region stress |
| `dsp/patched/ft8_mod1.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/ft8_mod1.f90` | Make `dd8` `THREADPRIVATE` | Independent deterministic band work buffers | Exact reversible transformation plus single/multithread multisignal set equivalence |
| `dsp/patched/ft8_decodevar.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/ft8_decodevar.f90` | Per-thread cycle 2/3 scratch; add deterministic A8 owner, barrier and atomic gate snapshot | Remove shared cycle/buffer/A8 races | Exact reversible transformations plus team-limit, A8 and cycle regressions |
| `dsp/patched/ft8_downsamplevar.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/ft8_downsamplevar.f90` | Make saved FFT cache `cxx` `THREADPRIVATE` | Prevent cross-band cache overwrite | Exact reversible transformation plus repeated parallel-region stress |
| `dsp/patched/ft8apsetvar.f90` | WSJT-X Improved 3.0.2 `lib/ft8var/ft8apsetvar.f90` | Clear and rebuild all AP masks for every headless request | Prevent call/grid/AP context leakage | Exact reversible transformations plus same-CDLL context-switch regression |

`tests/dsp/test_vendor_policy.py` also retains the independent full-tree vendor
digest gate, so these seven registered copies cannot mask a vendor edit.

Headless build adaptations outside `dsp/patched/` are also explicit:

| Local source | Origin | Adaptation and reason | Regression evidence |
|---|---|---|---|
| `dsp/ft8_stdcall.f90` | WSJT-X 3.0.2 `lib/qra/q65/q65_set_list.f90:66-97` | Equivalent extraction of `stdcall` into an independent compilation unit; the vendor unit also contains `q65_set_list`, which introduces `genq65` and the unrelated Q65 codec into the minimal FT8 library | Fresh configure/build in `tests/dsp/test_ft8_encode.py`; standard encode, error paths and exact 48 kHz/606,720-sample waveform |
