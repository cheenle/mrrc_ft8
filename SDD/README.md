# MRRC-FT8 System Design Description

IBM TeamSD-style design record for the headless FT8/FT4 server.

## Quick Facts

| Item | Value |
|---|---|
| SDD version | V1.0 |
| Date | 2026-08-01 |
| Phase | M1–M3 complete; deploy artifacts and NFR-002 latency histograms landed; FT-710 real-radio acceptance green (live decode + PTT/STOP) with two acceptance-driven fixes; Caddy/LaunchAgent install and Linux simulated acceptance remain |
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
