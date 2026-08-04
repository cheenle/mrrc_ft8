# 12. Operational Model

## 12.1 Runtime Topology

Caddy is the public service. FastAPI listens only on loopback and supervises the DSP Worker. rigctld listens only on loopback. Persistent data lives in a configurable local data directory with restricted permissions.

## 12.2 macOS

- User LaunchAgent starts MRRC-FT8 after the interactive user session is available.
- The user grants microphone/audio device permissions.
- Caddy owns the public TLS port and proxies to loopback FastAPI. (Reference topology keeps 80/443; the live deployment runs a root LaunchDaemon on 9988 with an operator-issued acme.sh DNS-01 certificate because inbound 80/443 are ISP-blocked.) The app's Host/Origin ACL (`MRRC_FT8_ALLOWED_HOSTS`) must list the public domain — it now includes `radio.vlsc.net` — or every public mutation/WebSocket is 403 and only GETs work.
- Release acceptance uses the real FT-710, USB audio, rigctld and PTT.

## 12.3 Linux

- systemd service uses an unprivileged account with explicit audio/device groups.
- Caddy owns 80/443 and proxies to loopback FastAPI.
- Release acceptance builds DSP, exercises service lifecycle, synthetic audio and mocked rigctld.
- Real-radio Linux validation is supported but does not block V1.0.

## 12.4 Startup

1. Load and validate config; refuse unsafe/public backend binding.
2. Open storage and apply explicit schema migrations.
3. Initialize safety state as PTT off / TX disarmed / no lease.
4. Connect rigctld and issue best-effort PTT off.
5. Start audio RX and DSP Worker; validate ABI/capabilities.
6. Start UTC orchestrator in monitor-only mode.
7. Serve loopback API for Caddy.

## 12.5 Shutdown and Restart

Priority STOP runs before audio/rig/worker teardown. An in-progress QSO becomes `ABORTED_RESTART`. Restart restores settings and history but not lease, PTT or armed TX.

## 12.6 Configuration

Configuration covers domain/proxy trust, password-hash bootstrap, radio/rigctld, audio devices, station identity, band table, decoder profile/threads, safety deadlines, storage/retention and logging. Secrets are never committed. Safety-impacting invalid values fail startup. V1.0 decoder defaults (I9): profile 3, threads Auto = `clamp(cpu_count - 1, 1, 12)`, decode-lateness cutoff slot end + 2.5 s. The reply TX decision window is `TX_DECISION_CUTOFF_SECONDS` (5.0) with a fit guard at ~2.4 s into the slot.

`MRRC_FT8_JTDX_LOG_PATH` (empty = disabled) points at the JTDX ADIF export (`~/FB/JTDX/wsjtx_log.adi`); the server imports it once at startup and then every hour, additive and idempotent — a missing file only logs a warning and the hourly tick retries. LOG surfaces (`/logs/qsos`, `/logs/adif`) are windowed to the last 7 days (NFR-085).

`cty.dat` (repo root, country-files ADIF format) is the DXCC entity source for `GET /api/v1/dxcc`; parsed lazily on first request (NFR-086).

Setting `auto_call_new_dxcc` (bool, persisted in setting_meta via `/settings`) arms unattended auto-QSO on the first new-DXCC CQ when idle; the safety interlock always gates TX (NFR-087).

## 12.7 Backup and Retention

- QSO database and configuration are backup-critical.
- Runtime logs rotate at 30 days, decodes at 7 days and audit at 90 days.
- QSO data has no automatic expiration.
- Diagnostic archives are raw and user-controlled; the UI warns before creation/download.

## 12.8 Operations and Troubleshooting

Health reports Caddy-visible application status, worker generation/restarts, decode latency/misses, audio overrun/underflow, waterfall drops, clock health, rig connection, PTT, lease and sequencer state. Operators resolve a fault, verify monitor state, reacquire the lease and manually re-arm; no recovery auto-resumes TX. The one automatic recovery is RX-side and monitor-only: a capture session that keeps the band hot yet decodes nothing for four consecutive slots (a silently degraded USB audio session never heals itself — 2026-08-02 field finding) is latched as an AUDIO fault and the capture stream is reopened automatically, at most three times per episode.

