"""Contract tests for the deployment artifacts and bootstrap CLI (§12).

The deploy files are verified textually so the suite runs without caddy,
systemd or launchd on the host.  The CLI test exercises the real
``python -m server.main --hash-password`` bootstrap path (§12.6).
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest
from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


@pytest.fixture(scope="module")
def caddyfile() -> str:
    return (DEPLOY / "Caddyfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def unit() -> str:
    return (DEPLOY / "mrrc-ft8.service").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def restart_sh() -> str:
    return (ROOT / "restart.sh").read_text(encoding="utf-8")


def test_caddyfile_proxies_to_loopback_fastapi(caddyfile: str) -> None:
    """§12.1: Caddy owns the edge; FastAPI stays on 127.0.0.1:8000."""

    assert "reverse_proxy 127.0.0.1:8000" in caddyfile
    assert "0.0.0.0" not in caddyfile


def test_caddyfile_defines_public_site_on_standard_ports(caddyfile: str) -> None:
    """NFR-030: one HTTPS site block (Caddy terminates TLS on 80/443)."""

    site_lines = [
        line for line in caddyfile.splitlines() if line and line.endswith(" {")
    ]
    assert len(site_lines) == 1
    assert site_lines[0].split(" ")[0] != ""


def test_systemd_unit_runs_unprivileged_with_audio_access(unit: str) -> None:
    """§12.3: unprivileged account with explicit audio/device groups."""

    assert "\nUser=mrrcft8\n" in f"\n{unit}"
    assert "User=root" not in unit
    assert "SupplementaryGroups=audio dialout" in unit
    assert "NoNewPrivileges=true" in unit


def test_systemd_unit_sets_omp_stacksize_and_entry(unit: str) -> None:
    """AGENTS.md: the worker needs OMP_STACKSIZE=10M before OpenMP loads."""

    assert "Environment=OMP_STACKSIZE=10M" in unit
    assert "ExecStart=" in unit and "-m server.main" in unit
    assert "0.0.0.0" not in unit


def test_systemd_unit_loads_secrets_from_env_file(unit: str) -> None:
    """§12.6: secrets never committed; the unit reads an env file."""

    assert "EnvironmentFile=/etc/mrrc-ft8.env" in unit
    assert "MRRC_FT8_PASSWORD_HASH=" not in unit


def test_launchagent_plist_shape() -> None:
    """§12.2: user LaunchAgent with OMP stack size and auto-restart."""

    plist = plistlib.loads((DEPLOY / "com.mrrc.ft8.plist").read_bytes())
    assert plist["Label"] == "com.mrrc.ft8"
    assert plist["EnvironmentVariables"]["OMP_STACKSIZE"] == "10M"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    args = plist["ProgramArguments"]
    assert args[-2:] == ["-m", "server.main"]
    assert "MRRC_FT8_PASSWORD_HASH" not in plist["EnvironmentVariables"]


def test_caddy_daemon_plist_shape() -> None:
    """§12.1: the edge runs as a root daemon so it can bind 80/443."""

    plist = plistlib.loads((DEPLOY / "com.caddyserver.caddy.plist").read_bytes())
    assert plist["Label"] == "com.caddyserver.caddy"
    assert plist["ProgramArguments"][:2] == ["/opt/homebrew/bin/caddy", "run"]
    assert "/etc/caddy/Caddyfile" in plist["ProgramArguments"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True


def test_restart_sh_serial_guard_blocks_conflicting_holders(
    restart_sh: str,
) -> None:
    """AD-008: rigctld must be the only serial owner.

    Field finding 2026-08-07: a stray ``server.py`` (old mrrc_ft710 web
    server) holding the CAT serial alongside rigctld caused ~90% rig timeouts
    for 4 h.  restart.sh must refuse to start rigctld when a non-rigctld
    process owns the device, with an explicit escape hatch.
    """

    assert "serial_guard()" in restart_sh
    assert 'lsof -t "$RIG_DEVICE"' in restart_sh
    assert "非 rigctld 进程持有串口" in restart_sh or "占用" in restart_sh
    assert "MRRC_FT8_SKIP_SERIAL_GUARD" in restart_sh
    assert "serial_guard || exit 1" in restart_sh


def test_hash_password_cli_prints_verifiable_argon2id() -> None:
    """§12.6 bootstrap: the CLI emits a hash that AuthService accepts."""

    result = subprocess.run(
        [sys.executable, "-m", "server.main", "--hash-password", "s3cret-test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    digest = result.stdout.strip()
    assert digest.startswith("$argon2id$")
    assert PasswordHasher().verify(digest, "s3cret-test")
