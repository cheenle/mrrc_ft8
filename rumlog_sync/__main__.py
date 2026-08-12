"""CLI entry: one sync round per invocation (crontab-driven, spec §11).

Exit codes: 0 = ok (including soft RUMLogNG-open failures, which log and
skip the round), 2 = bad config, 3 = another instance holds the lock.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no fcntl; use msvcrt byte-range locking.
    fcntl = None
    import msvcrt

from .config import RumlogConfig, load_config
from .sync import run_sync_once

log = logging.getLogger("rumlog_sync")


def _lock_exclusive(stream: object) -> None:
    """Advisory exclusive non-blocking lock (flock on Unix, msvcrt on Windows).

    Raises OSError when another process already holds the lock.
    """
    if fcntl is not None:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    stream.seek(0)
    stream.write("\0")  # msvcrt locks existing bytes only
    stream.flush()
    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rumlog_sync", description=__doc__)
    parser.add_argument(
        "--config",
        default="data/rumlog-sync.json",
        help="config path (default data/rumlog-sync.json)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="push one test QSO to RUMLogNG and exit (manual validation only)",
    )
    return parser.parse_args(argv)


def run_cli(argv: list[str], *, cwd: str | None = None) -> int:
    """Entry point returning a process exit code (0/2/3)."""

    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except ValueError:
        log.exception("config load failed")
        return 2

    lock_path = Path(cfg["lock_path"])
    if not lock_path.is_absolute():
        lock_path = Path(cwd or ".") / lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_stream:
        try:
            _lock_exclusive(lock_stream)
        except OSError:
            log.warning("another sync instance holds %s; skipping round", lock_path)
            return 3
        if args.smoke:
            _run_smoke(cfg)
            return 0
        report = run_sync_once(cfg)
        if report.errors:
            log.error("round errors: %s", report.errors)
        log.info(
            "round done: pulled=%d inserted=%d updated=%d pushed=%d requeued=%d",
            report.pulled, report.inserted, report.updated,
            report.pushed, report.requeued,
        )
        return 0


def _run_smoke(cfg: RumlogConfig) -> None:
    """Development validation: log a marker QSO via AppleScript (spec §6)."""

    from .apple_push import push_via_applescript

    rec = {
        "dx_call": "N0SMK",
        "dx_grid": "",
        "mode": "FT8",
        "band": "20m",
        "freq_hz": 14_074_000,
        "report_sent": None,
        "report_rcvd": None,
        "started_utc": "000000",
        "completed_epoch": 0.0,
        "my_call": cfg["my_call"],
    }
    # completed_epoch=0 renders a 1970 UTC timestamp — a clear marker;
    # delete it manually after validation.
    pushed = push_via_applescript([rec])
    if pushed:
        log.warning("smoke QSO sent to RUMLogNG — delete it manually after validation")
    else:
        log.error("smoke push failed")


if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
