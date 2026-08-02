# 3. Project Definition (ENG 343)

## 3.1 Attributes

| Attribute | Value |
|---|---|
| Name | MRRC-FT8 |
| Type | Headless FT8/FT4 server + landscape mobile Web remote |
| DSP | WSJT-X 3.0.2 Improved Fortran, C ABI, supervised worker |
| Backend | Python 3.11+, asyncio, FastAPI/Uvicorn |
| Radio | `rigctld` TCP; initial real target Yaesu FT-710 |
| Audio | PortAudio/sounddevice, 48 kHz hardware, 12 kHz decoder |
| Public edge | Caddy automatic TLS, FastAPI loopback only |
| Platforms | macOS LaunchAgent; Linux systemd |
| UI | Vanilla JS PWA, landscape full operation |

## 3.2 In Scope

- Minimal Fortran shim and shared `wsjt_core` library, including Improved `ft8var` OpenMP decoding.
- Supervised DSP Worker, binding lock, batch decode results and 48 kHz waveform generation.
- 48→12 kHz RX conversion, UTC ring buffer, waterfall and deadline-aware orchestration.
- rigctld client, audio RX/TX, central PTT watchdog and fail-safe startup/shutdown.
- Human-selected CQ/reply with one-QSO standard sequencer and three-retry policy.
- Automatic QSO persistence on RR73/73, audited void/undo, ADIF export.
- Caddy TLS, strong password sessions, Origin/Host protection and one control lease.
- REST/WS with bounded per-client queues and independent waterfall backpressure.
- qFT8-inspired landscape cockpit with WSJT-X semantics and low-frequency settings menus.
- Local health, retention, audit and password-gated raw diagnostic export.
- macOS real-radio and Linux simulated acceptance.

## 3.3 Out of Scope

- Continuous automatic target selection, Spy/Busy targeting and achievement-based AUTO strategy.
- Contest, Fox/Hound, SuperFox and non-FT8/FT4 modes.
- Direct serial ownership, Web-to-PTT commands, OpenMP-to-Python callbacks.
- TOTP/passkeys, multi-user roles, cloud telemetry, native mobile clients and Docker hardware support.

## 3.4 Success Criteria

| ID | Criterion | Verification |
|---|---|---|
| SC1 | `ft8sim` regression decodes known messages through standard and Improved paths | Automated DSP tests |
| SC2 | Landscape mobile browser completes one normal FT8 QSO and persists it | macOS real-radio test |
| SC3 | Profile 3 decode meets safe TX decision deadline at target thread setting | Latency histogram + deadline test |
| SC4 | Lease loss or controller disconnect during TX immediately stops audio/PTT | Fault injection + real radio |
| SC5 | Any authenticated observer can STOP TX without acquiring the lease | API/WS integration test |
| SC6 | CAT/audio/DSP fault and restart never auto-resume TX | Fault/restart tests |
| SC7 | Landscape UI preserves waterfall, candidates and bottom safety state on target phones | Responsive visual/interaction test |
| SC8 | Slow waterfall client does not delay state, heartbeat or STOP TX | Backpressure integration test |
| SC9 | macOS LaunchAgent + Caddy passes real FT-710 end-to-end acceptance | Deployment checklist |
| SC10 | Linux systemd + Caddy passes build/service/synthetic audio/mocked rig tests | CI/manual checklist |

## 3.5 Milestones

1. DSP ABI, `ft8var` and synthetic FT8 regression.
2. Audio/UTC engine, rigctld, sequencer and TX safety.
3. Auth/lease/API and landscape Web vertical slice.
4. macOS real-radio acceptance.
5. Linux simulated acceptance.
6. FT4 extension and regression.

