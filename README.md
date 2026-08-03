# MRRC-FT8

Headless FT8/FT4 server using the WSJT-X 3.0.2 Improved DSP core, a Python FastAPI control plane, rigctld radio control and a landscape mobile Web cockpit.

v0.1.0 is live on the BG1SB/FT-710 station (edge `https://radio.vlsc.net:9988`), serving real FT8 QSOs after the 2026-08-03 field session closed the RX/TX root causes (UTC-ring eviction misalignment, Replay opposite-slot phase, manual-Reply decision window).

## Architecture Rules

- Decoder input: 12 kHz int16 mono; TX: 48 kHz.
- DSP runs only in a supervised Worker through `server/core/binding.py` and its global lock.
- `rigctld` is the sole CAT serial owner.
- TX runs only through the sequencer and central PTT safety controller.
- Public access terminates at Caddy; FastAPI and rigctld remain loopback-only.
- Multiple sessions may observe; one control lease may start TX; any authenticated session may STOP TX.
- `wsjtx-3.0.2/` is immutable vendor reference source.

## Design

- [SDD quick facts](SDD/README.md)
- [Approved design spec](docs/superpowers/specs/2026-08-01-mrrc-ft8-headless-server-design.md)
- [Contributor instructions](AGENTS.md)

Implementation and run commands in `AGENTS.md` describe the target repository workflow and will be verified milestone by milestone.
