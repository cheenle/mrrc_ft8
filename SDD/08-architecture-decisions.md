# 8. Architecture Decisions

## AD-001 — Headless layered server

**Decision:** Separate Web, engine, core/DSP and persistence responsibilities. No Qt or desktop GUI runtime is required.  
**Consequence:** Mobile and desktop clients share one authoritative backend.

## AD-002 — Compile selected WSJT-X Improved sources behind a C ABI

**Decision:** Reuse vendor DSP through Fortran `bind(C)` shims and a shared library.  
**Consequence:** Algorithm behavior stays close to WSJT-X; ABI and build regression become release gates.

## AD-003 — Supervised DSP Worker process

**Decision:** All Fortran/OpenMP operations execute in a separate worker supervised by the main process.  
**Consequence:** Main security/PTT logic survives worker faults; IPC copies are accepted for isolation.

## AD-004 — Fixed audio rate domains

**Decision:** Radio audio and TX are 48 kHz; decoder input is 12 kHz int16 mono, with exactly one RX conversion.  
**Consequence:** Binding rejects all other decode formats.

## AD-005 — Single DSP owner, lock and batch results

**Decision:** The worker calls DSP only through the global binding lock. Fortran aggregates OpenMP output and returns a batch; no Python callbacks from worker threads.  
**Consequence:** Mutable packjt77/hash state is serialized and callback/GIL hazards are removed.

## AD-006 — UTC epoch slot discipline

**Decision:** Slot identity is `floor(epoch/TRperiod)` and late actions are skipped.  
**Consequence:** Tests inject epoch time; relative timers cannot define protocol phase.

The UTC ring keys physical positions to the absolute sample index (`X % capacity`),
so eviction (advancing the oldest-retained base) never requires moving data — a
base-relative key would leave every post-eviction read shifted by the cumulative
base advance (2026-08-03 field bug "D").

## AD-007 — Central sequencer/PTT safety controller

**Decision:** Every TX intent crosses sequencer validation, health interlocks and the main-process watchdog.  
**Consequence:** DSP and Web layers cannot key PTT directly; STOP is a priority idempotent operation.

## AD-008 — rigctld is the serial owner

**Decision:** The application uses Hamlib TCP and never opens the CAT serial device.  
**Consequence:** Radio compatibility is isolated; rigctld failure is a handled interlock.

## AD-009 — Caddy public edge and secure-cookie auth

**Decision:** Caddy terminates public TLS; FastAPI binds loopback. Sessions use secure cookies, not URL tokens, with Host/Origin validation.  
**Consequence:** Deployment requires a domain and correct proxy headers.

## AD-010 — Single renewable control lease

**Decision:** Multiple sessions may observe, but only one 5 s heartbeat/15 s TTL lease can issue transmit-starting commands. Any authenticated session may STOP.  
**Consequence:** No last-writer-wins radio control; lease loss during TX invokes safe stop.

## AD-011 — Landscape-first cockpit

**Decision:** Full mobile operation is landscape with left waterfall, right candidates and fixed bottom state machine. Portrait is a degraded observer/STOP view.  
**Consequence:** Responsive acceptance explicitly covers both modes.

## AD-012 — Human target selection, single-QSO automation

**Decision:** The user selects CQ or Reply; the sequencer completes one standard QSO, permits one initial send plus three retransmissions per message, and then disarms.  
**Consequence:** qFT8-style continuous target selection, Spy/Busy and contests are deferred.

## AD-013 — Separate bounded streams

**Decision:** Safety/state, decode events and waterfall use separate bounded WebSocket delivery paths. Old waterfall frames may drop.  
**Consequence:** Visual congestion cannot block lease/STOP/state traffic.

## AD-014 — SQLite canonical records with ADIF export

**Decision:** QSO/audit/history are transactional local data; ADIF is generated. Undo marks a QSO void with audit evidence.  
**Consequence:** Persistence gains schema migration and backup responsibilities.

## AD-015 — Native macOS/Linux service deployment

**Decision:** macOS uses a user LaunchAgent and real hardware acceptance; Linux uses systemd and simulated acceptance; Caddy is shared, Docker deferred.  
**Consequence:** Service descriptors differ but application config and code stay common.
