"""RUMLogNG AppleScript push channel (AD-016).

RUMLogNG exposes a native AppleScript API for logging QSOs: set application
properties (callsign/mode/frequency/rst*/locator/logDateTime) then call
``logQSO``.  Unlike the WSJT-X UDP 2237 socket (which RUMLogNG may not have
activated), AppleScript is an official, always-available write path.  One
``osascript`` invocation can log many QSOs sequentially, so pushes are
batched per round.  ``frequency`` is kHz; ``logDateTime`` is UTC
``yyyy-MM-dd HH:mm:ss`` (verified against the RUMLogNG store).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from typing import Any, Protocol

log = logging.getLogger(__name__)

_OSASCRIPT = "/usr/bin/osascript"
# RUMLogNG's AppleScript handler is slow (seconds per QSO); keep each
# osascript invocation small so a batch fits well inside the timeout.
DEFAULT_BATCH = 15


class _Proc(Protocol):
    """The result surface we read after a push (returncode/stderr)."""

    returncode: int
    stderr: str | None


class _Runner(Protocol):
    """The subprocess.run surface we need (injectable for tests)."""

    def __call__(
        self,
        cmd: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
    ) -> _Proc: ...


def _f(value: Any) -> float:
    """Coerce a DB value to float; garbage → 0.0."""

    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _i(value: Any) -> int | None:
    """Coerce a DB value to int; garbage → None."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _esc(value: str) -> str:
    """Escape a string for an AppleScript literal."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def _set_line(name: str, value: str) -> str:
    return f'set {name} to "{_esc(value)}"'


def build_applescript(records: list[dict[str, object]]) -> str:
    """One AppleScript block logging every record (UTC logDateTime, kHz)."""

    lines = ["tell application \"RUMlogNG\""]
    for rec in records:
        epoch = _f(rec.get("completed_epoch"))
        utc = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        freq_khz = _f(rec.get("freq_hz")) / 1000.0
        lines.append(_set_line("callsign", str(rec.get("dx_call") or "")))
        lines.append(_set_line("mode", str(rec.get("mode") or "")))
        lines.append(f"set frequency to {freq_khz:.3f}")
        rst_tx = _i(rec.get("report_sent"))
        lines.append(_set_line("rstTX", "" if rst_tx is None else f"{rst_tx:+03d}"))
        rst_rx = _i(rec.get("report_rcvd"))
        lines.append(_set_line("rstRX", "" if rst_rx is None else f"{rst_rx:+03d}"))
        lines.append(_set_line("locator", str(rec.get("dx_grid") or "")))
        lines.append(_set_line("logDateTime", utc))
        lines.append("logQSO")
    lines.append("end tell")
    return "\n".join(lines)


def push_via_applescript(
    records: list[dict[str, object]],
    *,
    runner: _Runner | None = None,
    batch_size: int = DEFAULT_BATCH,
) -> list[dict[str, object]]:
    """Log records via osascript batches; returns the successfully pushed ones.

    Records are pushed in small batches (each its own osascript subprocess)
    because RUMLogNG processes each QSO slowly.  A failed batch is logged and
    its records are omitted from the result so the caller keeps them queued
    for the next round (spec §7).
    """

    if not records:
        return []
    run = runner if runner is not None else subprocess.run
    pushed: list[dict[str, object]] = []
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        script = build_applescript(batch)
        try:
            proc = run(
                [_OSASCRIPT, "-e", script],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            log.exception("osascript push failed for %d record(s)", len(batch))
            continue
        if proc.returncode != 0:
            log.error(
                "osascript push failed (%d): %s",
                proc.returncode,
                (proc.stderr or "").strip(),
            )
            continue
        pushed.extend(batch)
    log.info("applescript push ok: %d/%d record(s)", len(pushed), len(records))
    return pushed
