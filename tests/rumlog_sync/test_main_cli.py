"""__main__: CLI entry, flock guard, exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from rumlog_sync.__main__ import run_cli


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "rumlog-sync.json"
    cfg.write_text(
        json.dumps(
            {
                "ft8_db": str(tmp_path / "mrrc-ft8.db"),
                "rumlog_db": str(tmp_path / "missing.sqlite"),  # pull fails softly
                "lock_path": str(tmp_path / "sync.lock"),
            }
        )
    )
    return cfg


def test_run_cli_soft_error_on_missing_rumlog_db(tmp_path: Path) -> None:
    # Explicit config avoids touching the real RUMLogNG store; its
    # rumlog_db points at a missing file → soft error, exit 0.
    cfg = _write_config(tmp_path)
    code = run_cli(["--config", str(cfg)], cwd=str(tmp_path))
    assert code == 0


def test_run_cli_bad_config_exits_nonzero(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.json"
    cfg.write_text("{nope")
    code = run_cli(["--config", str(cfg)], cwd=str(tmp_path))
    assert code == 2


def test_run_cli_flock_prevents_concurrent_run(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    lock = tmp_path / "sync.lock"
    with lock.open("w") as stream:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = run_cli(["--config", str(cfg)], cwd=str(tmp_path))
        assert code == 3  # another instance holds the lock


def test_module_runs_as_program() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "rumlog_sync", "--help"],
        capture_output=True,
        text=True,
        cwd="/Users/cheenle/HAM/ft8",
    )
    assert proc.returncode == 0
    assert "--config" in proc.stdout
