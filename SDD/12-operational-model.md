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

`data/device-config.json` (operator-editable, created by `PUT /api/v1/devices`) drives rigctld launch parameters and the server's audio device selection at startup. When present, its values take precedence over environment variables. `restart.sh` reads it before spawning rigctld; the server reads it during `server/engine/device_config.py:merge_into` to override `audio_device` and `rigctld_port` in `ServerConfig`. Fields and priority:

| Field | Type | Env fallback |
| --- | --- | --- |
| `rig_model` | int (hamlib model no.) | `MRRC_FT8_RIG_MODEL` |
| `rig_device` | str (serial path) | `MRRC_FT8_RIG_DEVICE` |
| `rig_baud` | int | `MRRC_FT8_RIG_BAUD` |
| `rigctld_port` | int | `MRRC_FT8_RIGCTLD_PORT` (restart.sh) / `MRRC_FT8_RIGCTLD` host:port (server) |
| `audio_device` | int or str | `MRRC_FT8_AUDIO_DEVICE` |

Priority: file value > env var > built-in default. The file contains no secrets; file values are round-tripped through `GET /api/v1/devices` and the UI's Settings → Devices tab (desktop + PWA). Atomic writes via tempfile+rename ensure the file is never half-written.

## 12.7 Backup and Retention

- QSO database and configuration are backup-critical.
- Runtime logs rotate at 30 days, decodes at 7 days and audit at 90 days.
- QSO data has no automatic expiration.
- Diagnostic archives are raw and user-controlled; the UI warns before creation/download.

**12.7.1 RUMLogNG sync state** — rumlog_sync（AD-016）在 qso 表维护 `rumlog_uuid`（RUMLogNG 侧 UUID）与 `pushed_to_rumlog`（推送状态），`rumlog_sync_state` 表存 Z_PK 游标与未确认推送计数；迁移幂等（PRAGMA 探测 + ADD COLUMN），与服务器 Repository 显式列查询向后兼容。

## 12.8 Operations and Troubleshooting

Health reports Caddy-visible application status, worker generation/restarts, decode latency/misses, audio overrun/underflow, waterfall drops, clock health, rig connection, PTT, lease and sequencer state. Operators resolve a fault, verify monitor state, reacquire the lease and manually re-arm; no recovery auto-resumes TX. The one automatic recovery is RX-side and monitor-only: a capture session that keeps the band hot yet decodes nothing for four consecutive slots (a silently degraded USB audio session never heals itself — 2026-08-02 field finding) is latched as an AUDIO fault and the capture stream is reopened automatically, at most three times per episode.

In addition, a **proactive band-switch capture restart** (2026-08-05) prevents the degradation episode before it starts: the FT-710's C-Media USB codec can silently wedge its RX stream when the radio rebuilds its DSP/audio path across a band change (observed live: band switches at 10:25/10:26 produced UTC-ring gaps and a hot-but-zero-decode session that latched AUDIO 60 s later). When `rig_poll` or the band-hunter observes the dial frequency move to a different FT8 band, the capture child is reopened immediately (fresh streams are always clean), at most once per band, deferred while PTT is on. `CaptureProcess.healthy` is now locked against `restart()` so the watchdog can never double-restart behind the teardown→spawn window (field finding 2026-08-05).

Because a fresh stream can still show hot-but-zero-decode slots when the band simply carries no FT8 content (e.g. strong phone traffic on 40 m at night — 2026-08-05 field finding: band switched to 40 m at 22:11, AUDIO latched at 22:12, capture restarted yet still zero decodes until returning to 20 m at 23:39), the automatic recovery now runs a **re-verify window** after each restart: two consecutive hot-and-silent slots on the reopened stream clear the AUDIO fault automatically as a false positive. Operators still re-arm TX manually, so no recovery auto-resumes TX.
