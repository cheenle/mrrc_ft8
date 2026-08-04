# 7. Subject Area Model

## 7.1 Core Domains

| Domain | Entities | Owner |
|---|---|---|
| Identity | Session, Reauthentication | Main process auth service |
| Authority | ControlLease, Heartbeat, CommandIntent | Lease manager |
| Radio | RigState, AudioState, PttState, BandMode | Engine/safety controller |
| Timing | Slot, SlotPhase, Deadline, ClockHealth | UTC orchestrator |
| DSP | DecodeRequest, DecodeBatch, EncodeRequest, Waveform | DSP Worker |
| Operation | Candidate, SelectedStation, QsoContext, TxMessage | Sequencer |
| Persistence | QsoRecord, AuditEvent, DecodeHistory, Setting | Repository layer |
| Presentation | StateRevision, DecodeEvent, SpectrumFrame | Web broadcaster |

## 7.2 State Ownership

No entity has two mutable owners. Browsers hold read-only mirrors and submit intent. The DSP Worker owns no PTT, lease or QSO state. `rigctld` owns the serial descriptor but not the application RigState mirror.

## 7.3 Principal State Machines

### Control lease

```text
FREE -> HELD -> EXPIRED/RELEASED -> FREE
             -> DISCONNECTED ----> FREE
```

### QSO

```text
MONITOR -> SELECTED/CQ_READY -> TX_ARMED
 -> REPLYING -> REPORT -> ROGER_REPORT -> ROGERS -> SIGNOFF
 -> COMPLETED -> MONITOR

active -> RETRY_EXHAUSTED | STOPPED | FAULT | ABORTED_RESTART -> MONITOR
```

### DSP Worker

```text
STARTING -> READY -> BUSY -> READY
    |         |       |
    +------> FAILED <-+
                 -> RESTARTING -> READY
```

## 7.4 Invariants

- `PttState == ON` implies a live safety-controller transmission context.
- `TxArmed == true` implies a valid control lease and healthy CAT/audio/DSP/clock.
- QSO completion or any terminal error makes `TxArmed == false`.
- A slot is identified by UTC epoch math, not local timer count.
- A DecodeRequest contains 12 kHz mono int16 only.
- A Waveform contains 48 kHz mono samples only.
- A void QSO remains auditable.

## 7.5 Persistence Model

SQLite is canonical. Minimum tables are `qso`, `qso_event`, `audit_event`, `decode_event`, `setting_meta` and schema migration metadata. Session secrets remain in a dedicated security store/runtime and are never included in diagnostics. ADIF is a generated interoperability artifact, not the sole database.

The `qso` row carries a `source` attribute: `'live'` (completed through the sequencer) or `'jtdx'` (imported from the JTDX ADIF export). The JTDX import path (`server/engine/adif_import.py`) is additive and idempotent — a record is skipped when its dedupe key `(dx_call, utc date, started_utc, band)` already exists in any source, so a live QSO is never re-imported and re-syncs only add new rows.

