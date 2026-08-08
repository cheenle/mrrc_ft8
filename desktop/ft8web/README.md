# MRRC-FT8 Desktop Client

A desktop FT8 interface for the [MRRC-FT8](/) station: the ft8web UI shell
(React 19 + Vite + Tailwind CSS v4) wired to the MRRC-FT8 headless server as
its brain. This is an adapted fork of [ok1cdj/FT8web](https://github.com/ok1cdj/FT8web).

## How it differs from upstream

- **No browser DSP/audio.** All FT8 decoding, radio/CAT control, TX (PTT), and
  logging run on the MRRC-FT8 server. The browser is a control panel only — it
  needs no microphone or sound-card access, and it never transmits by itself.
- **No local FSM / CAT / PWA / cloud-logging UI.** Those upstream features are
  gone; the server owns the QSO sequencer, radio, and external logging
  integrations. This client controls the FT8 session via the server's REST +
  WebSocket API (`/api/v1`, `/ws/v1/*`).

## Prerequisites

- A running MRRC-FT8 server (`OMP_STACKSIZE=10M venv/bin/python -m server.main`
  from the repo root) that mounts this build at **`/desktop`** (see the server
  plan's "desktop client" tasks). The client serves its assets from
  `/desktop/`, so the build base path is `/desktop/`.

## Dev workflow

```bash
npm install
npm run dev      # vite on :3000; proxies /api and /ws to 127.0.0.1:8000
npm run build    # production build into dist/ (base /desktop/)
npm test         # vitest: mrrcClient + mrrcStreams + pskReporterSpot suites
npm run lint     # tsc --noEmit
```

`npm run dev` assumes the MRRC-FT8 server is already listening on
`127.0.0.1:8000` — the Vite proxy forwards `/api` (HTTP) and `/ws` (WebSocket)
traffic there.

## Deploy to the station

`dist/` is gitignored, so a `git pull` alone does **not** bring the built
client — the station must build it before restarting the server:

```bash
cd /path/to/mrrc_ft8
git pull
cd desktop/ft8web
npm install          # first deploy only (or when deps change)
npm run build        # → dist/ with base /desktop/
cd ../..
# restart the server (restart.sh, or your LaunchAgent/systemd unit)
```

The client is then served at `<server-origin>/desktop/` (e.g.
`https://radio.vlsc.net:9988/desktop/`). The mobile PWA stays at `/static/`.

## Operation

- **Login** with the station password, then select a band (40/20/15/10 m).
- **Band Activity**: single-click a decode row **selects** it (never transmits —
  server §15.6); **double-click** replies (the mobile-PWA convention). The
  `Ans` button replies to the selected station; `CQ` starts a CQ call; `STOP`
  always works (no lease needed).
- **Control lease**: CQ/Ans/band-change acquire the lease implicitly; the
  client heartbeats every 5 s while holding it (server §15.4, TTL 15 s — do not
  raise the interval, or the dead-man STOP fires mid-transmission).
- TX messages appear in the Active QSO panel from the server's `last_tx`
  snapshot; the server sequencer runs the whole QSO (grid → report → RR73 → 73)
  and logs it — the client only sends intents.

## Server settings this client exposes

The Settings modal reads and writes these server keys via `/api/v1/settings`:

- `decoder_profile` (0–4) and `decoder_threads` (1–12; 0 = Auto) — decode depth
  and thread count.
- `auto_call_new_dxcc` — auto-call a not-yet-worked DXCC entity.
- `auto_band_hunt` — hunt new DXCC entities across bands.

Station identity (callsign/grid), radio/CAT, audio, and logging integrations
are configured on the server, not in this client.

## License

GNU General Public License v3 (GPL v3). Adapted from
[ok1cdj/FT8web](https://github.com/ok1cdj/FT8web).
