# 4. System Context (APP 011)

## 4.1 Context Diagram

```text
Authenticated Browser
        |
        | HTTPS / WSS
        v
      Caddy
        |
        | loopback
        v
FastAPI Main Process ---------------------- SQLite / ADIF / local logs
  | auth, lease, REST/WS
  | UTC, audio, sequencer, PTT
  |
  +-- supervised IPC --> DSP Worker --> wsjt_core
  +-- loopback TCP ----> rigctld -----> CAT serial -----> Radio
  +-- PortAudio 48 kHz ---------------------------------> Radio USB audio
```

## 4.2 Actors

| Actor | Responsibility |
|---|---|
| HAM Operator | Observes, acquires control, selects station/CQ, stops TX, reviews logs |
| Authenticated Observer | Observes and can always issue emergency STOP TX |
| Caddy | Public TLS termination and reverse proxy |
| FastAPI main | Security, ownership, state, audio, rig, PTT and persistence |
| DSP Worker | Sole Fortran/OpenMP owner; decode/encode only |
| rigctld | Sole CAT serial owner |
| Radio | RF and USB audio endpoint |

## 4.3 External Interfaces

| Boundary | Interface | Constraint |
|---|---|---|
| Browser–Caddy | HTTPS/WSS | Secure cookie; expected Host/Origin |
| Caddy–FastAPI | loopback HTTP/WS | FastAPI is not public |
| Main–Worker | supervised local IPC | One outstanding decode; bounded request/result size |
| Main–rigctld | loopback Hamlib TCP | No direct serial open |
| Main–radio audio | PortAudio 48 kHz mono | One audio owner |
| Worker–library | ctypes C ABI | Binding lock; 12 kHz int16 decode input |
| Main–storage | SQLite/files | Local only; transactional canonical data |

## 4.4 Data Flows

### Receive

Radio 48 kHz audio → deterministic 4:1 conversion → UTC-aligned 12 kHz int16 ring → waterfall frames and slot decode window → worker → batched decode results → candidate model → Web clients.

### Transmit

Leased operator intent → sequencer validation → worker encode/generate 48 kHz waveform → slot eligibility → PTT safety controller → radio audio/PTT. Any interlock failure cancels the chain.

### State

Rig/audio/DSP/slot/sequencer/lease state → revisioned state stream. Waterfall uses a separate bounded stream so spectrum loss cannot block safety data.

## 4.5 Trust Boundaries

- The public network ends at Caddy.
- Browsers are untrusted until authenticated; authenticated sessions still need a lease for transmit-starting actions.
- DSP output is data, never authority to transmit.
- rigctld and audio devices are local privileged resources.
- Diagnostic archives are sensitive local exports and require password re-authentication.

## 4.6 Resource Ownership

| Resource | Owner |
|---|---|
| Session and lease | Main process |
| QSO/sequencer | Main process |
| PTT and audio | Main process |
| Fortran/OpenMP/hash state | DSP Worker |
| Serial device | rigctld |
| Vendor source | Read-only repository content |

