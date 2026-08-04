# MRRC-FT8

Headless FT8/FT4 server — WSJT-X 3.0.2 Improved DSP core, Python FastAPI control plane, rigctld radio control, landscape mobile Web cockpit.

**Live** on the FT-710 station (`https://radio.vlsc.net:9988`). The 2026-08-03 field session closed the RX/TX root causes; on 2026-08-04 the FT-710 rig controls were completed (filter + ATT/PREAMP/AGC/RF via raw CAT, verified live on hamlib 4.7.2).

## Quick install

```bash
git clone https://github.com/cheenle/mrrc_ft8 && cd mrrc_ft8
python3 -m venv venv && venv/bin/pip install -e '.[dev]'   # deps (needs gfortran-mp-13 + FFTW3f)
cmake -S dsp -B dsp/build -DCMAKE_Fortran_COMPILER=gfortran-mp-13 && cmake --build dsp/build -j
venv/bin/python -m server.main --hash-password             # → value for MRRC_FT8_PASSWORD_HASH
# write .env: MRRC_FT8_PASSWORD_HASH / MRRC_FT8_MY_CALL / MRRC_FT8_MY_GRID
#   optional: MRRC_FT8_ALLOWED_HOSTS / MRRC_FT8_AUDIO_DEVICE
OMP_STACKSIZE=10M venv/bin/python -m server.main           # loopback :8000
```

The server refuses to start without the hash/call/grid secrets. Point a browser at the cockpit, log in, select a band and run a QSO.

## Deploy

- **Service**: `deploy/com.mrrc.ft8.plist` (macOS LaunchAgent) or `deploy/mrrc-ft8.service` (Linux systemd).
- **Public edge**: `deploy/Caddyfile` — Caddy owns 80/443 and proxies to the loopback app; set `MRRC_FT8_ALLOWED_HOSTS` to the public domain.
- **Website**: `website/deploy.sh`.

## Architecture rules (abridged)

- `rigctld` is the sole CAT serial owner; the app never touches serial (AD-008).
- TX runs only through the sequencer and the central PTT safety controller.
- Public access ends at Caddy; FastAPI and rigctld stay loopback-only.
- DSP runs only in the supervised Worker through `server/core/binding.py` and its global lock.
- Many sessions may observe; one control lease may start TX; any authenticated session may STOP.

## Design

- [SDD quick facts](SDD/README.md) · [Design spec](docs/superpowers/specs/2026-08-01-mrrc-ft8-headless-server-design.md) · [Contributor guide](AGENTS.md)
