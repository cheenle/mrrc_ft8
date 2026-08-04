# MRRC-FT8 System Design Description

IBM TeamSD-style design record for the headless FT8/FT4 server.

## Quick Facts

| Item | Value |
|---|---|
| SDD version | V1.2 |
| Date | 2026-08-04 |
| Phase | Live on `radio.vlsc.net:9988` (FT-710). 2026-08-03 field session closed the open RX/TX root causes: UtcRing eviction misalignment (absolute-index keying), Replay opposite-TX-slot phase (UC-003) and the manual-Reply decision window (polling + 5 s cutoff + fit guard). Public Host ACL opened so the Caddy edge reaches the full API. Waterfall span reduced to 3 kHz; FT8 band selector added. Repo tidied and pushed as v0.1.0 (vendor source untracked, kept on disk for builds). **V1.2 (2026-08-04):** after the hamlib 4.7.2 upgrade the drawer's FT-710 rig controls were completed — filter bandwidth via raw `SH00<NN>;` (hamlib 4.6.2 mis-framed it) and ATT/PREAMP/AGC/RF gain via raw `\send_raw` CAT frames (hamlib's `L`/`l` level path is unreliable on the FT-710; AGC AUTO is `GT06;`, not the `GT04;` hamlib sends). All verified live against the station rigctld; TX power is not CAT-controllable on the FT-710. **v1.0.0 (2026-08-04):** first public release — real FT8 QSOs on the FT-710 station. |
| First vertical slice | Normal FT8 QSO |
| DSP | WSJT-X 3.0.2 Improved, supervised worker, `ft8var` OpenMP |
| Public edge | Caddy TLS; FastAPI loopback |
| Control | Many sessions, one lease, universal authenticated STOP |
| Deployment | macOS LaunchAgent + real hardware; Linux systemd + simulation |

## Chapters

1. Executive Summary
2. Business Direction
3. Project Definition
4. System Context
5. Non-Functional Requirements
6. Use Case Model
7. Subject Area Model
8. Architecture Decisions
9. Architecture Overview
10. Service Model
11. Component Model
12. Operational Model
13. Feasibility Assessment
14. Version History
15. PTT Safety Architecture

`SDD/` is canonical for the approved architecture. The brainstorming spec in `docs/superpowers/specs/` records the proposal and rationale. Runtime deviations must update both the affected chapter and chapter 14.
