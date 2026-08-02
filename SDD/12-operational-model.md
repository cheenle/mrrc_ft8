# 12. Operational Model

## 12.1 Runtime Topology

Caddy is the public service. FastAPI listens only on loopback and supervises the DSP Worker. rigctld listens only on loopback. Persistent data lives in a configurable local data directory with restricted permissions.

## 12.2 macOS

- User LaunchAgent starts MRRC-FT8 after the interactive user session is available.
- The user grants microphone/audio device permissions.
- Caddy owns the public TLS port and proxies to loopback FastAPI. (Reference topology keeps 80/443; the BG1SB deployment runs a root LaunchDaemon on 9988 with an operator-issued acme.sh DNS-01 certificate because inbound 80/443 are ISP-blocked.)
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

Configuration covers domain/proxy trust, password-hash bootstrap, radio/rigctld, audio devices, station identity, band table, decoder profile/threads, safety deadlines, storage/retention and logging. Secrets are never committed. Safety-impacting invalid values fail startup. V1.0 decoder defaults (I9): profile 3, threads Auto = `clamp(cpu_count - 1, 1, 12)`, TX decision cutoff slot end + 2.5 s.

## 12.7 Backup and Retention

- QSO database and configuration are backup-critical.
- Runtime logs rotate at 30 days, decodes at 7 days and audit at 90 days.
- QSO data has no automatic expiration.
- Diagnostic archives are raw and user-controlled; the UI warns before creation/download.

## 12.8 Operations and Troubleshooting

Health reports Caddy-visible application status, worker generation/restarts, decode latency/misses, audio overrun/underflow, waterfall drops, clock health, rig connection, PTT, lease and sequencer state. Operators resolve a fault, verify monitor state, reacquire the lease and manually re-arm; no recovery auto-resumes TX.

