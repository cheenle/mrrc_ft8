# MRRC-FT8 System Design Description

IBM TeamSD-style design record for the headless FT8/FT4 server.

## Quick Facts

| Item | Value |
|---|---|
| SDD version | V1.7 |
| Date | 2026-08-10 |
| Phase | Live on `radio.vlsc.net:9988` (FT-710). 2026-08-03 field session closed the open RX/TX root causes: UtcRing eviction misalignment (absolute-index keying), Replay opposite-TX-slot phase (UC-003) and the manual-Reply decision window (polling + 5 s cutoff + fit guard). Public Host ACL opened so the Caddy edge reaches the full API. Waterfall span reduced to 3 kHz; FT8 band selector added. Repo tidied and pushed as v0.1.0 (vendor source untracked, kept on disk for builds). **V1.2 (2026-08-04):** after the hamlib 4.7.2 upgrade the drawer's FT-710 rig controls were completed — filter bandwidth via raw `SH00<NN>;` (hamlib 4.6.2 mis-framed it) and ATT/PREAMP/AGC/RF gain via raw `\send_raw` CAT frames (hamlib's `L`/`l` level path is unreliable on the FT-710; AGC AUTO is `GT06;`, not the `GT04;` hamlib sends). All verified live against the station rigctld; TX power is not CAT-controllable on the FT-710. **v1.0.0 (2026-08-04):** first public release — real FT8 QSOs on the FT-710 station. **v1.1.0 (2026-08-05):** DXCC live view + new-DXCC auto-call — JTDX ADIF auto-sync + 7-day LOG window (NFR-085), in-cockpit DXCC stats cached until QSO write (NFR-086), new-DXCC highlight + safety-armed auto-call toggle (NFR-087), full-screen QSO Log overlay. **V1.3 (2026-08-07):** TX frequency discipline — a Reply is encoded on the audio offset the partner was decoded at (UC-003 RX-offset half closed; sequencer-owned `tx_frequency`, auto-call/select/reply plumb `freq`, legacy default 1500 Hz), and a CQ picks an unoccupied offset near 1500 Hz from the live decode occupancy (UC-004; `FrequencyOccupancy` + `pick_cq_frequency`, 30 Hz guard, default fallback). Field regression: TN8GD auto-call at 843 Hz was answered at 1500 Hz and never paired. **V1.4 (2026-08-07):** serial-owner guard — `restart.sh` refuses to start rigctld while a non-rigctld process holds the CAT serial (AD-008; field finding: a stray mrrc_ft710 `server.py` caused 4 h of ~90% rig timeouts); `MRRC_FT8_SKIP_SERIAL_GUARD=1` escape hatch. Old-project `switch.sh`/`stop.sh` hardened (case-insensitive `pgrep -if`, serial-holder fallback). **V1.5 (2026-08-08):** desktop client — FT8web-derived UI (`desktop/ft8web/`, GPL v3) with the server as its brain, served at `/desktop`. Server gains two additive changes: `TxDriver.on_transmitted` observer + `last_tx` in the state snapshot (desktop Active QSO shows sent messages). Client does no local DSP/audio/PTT; it drives the server REST + 3 WebSockets, takes the control lease implicitly, and heartbeats every 5 s per §15.4. `last_tx`/`since_days`/`dxcc_entity` are additive server fields the mobile PWA ignores. Field fixes: lease heartbeat 15 s→5 s (mid-TX dead-man STOP), decode-row double-click→reply. **V1.7 (2026-08-10):** station device configuration — HAMLIB rig (model/serial/baud/rigctld port) and the audio device are now configurable from the desktop Settings modal and the PWA ☰ Devices tab; `data/device-config.json` (file > env > default) drives both the server's audio/rigctld connection and `restart.sh`'s rigctld launch; Save persists without restart, Apply & Restart relaunches rigctld + server (~20 s disconnect, then re-login). AD-008 preserved: rigctld remains the serial owner. |
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
