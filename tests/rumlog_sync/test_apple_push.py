"""apple_push: AppleScript generation + osascript runner (mock, no side effects)."""

from __future__ import annotations

import subprocess

from rumlog_sync.apple_push import build_applescript, push_via_applescript

REC: dict[str, object] = {
    "dx_call": "TL8GD",
    "dx_grid": "PM02",
    "mode": "FT8",
    "freq_hz": 14_074_684,
    "report_sent": -12,
    "report_rcvd": -12,
    "completed_epoch": 1_786_404_209.0,  # 2026-08-10 23:23:29 UTC
    "my_call": "BG1SB",
}


def test_build_applescript_single_record() -> None:
    script = build_applescript([REC])
    assert script.startswith('tell application "RUMlogNG"')
    assert script.endswith("end tell")
    assert 'set callsign to "TL8GD"' in script
    assert 'set mode to "FT8"' in script
    assert "set frequency to 14074.684" in script
    assert 'set rstTX to "-12"' in script
    assert 'set rstRX to "-12"' in script
    assert 'set locator to "PM02"' in script
    assert 'set logDateTime to "2026-08-10 23:23:29"' in script
    assert script.count("logQSO") == 1


def test_build_applescript_batches_and_escapes() -> None:
    rec2 = dict(REC, dx_call='WE"IRD', report_sent=None, report_rcvd=None)
    script = build_applescript([REC, rec2])
    assert script.count("logQSO") == 2
    assert 'set callsign to "WE\\"IRD"' in script
    assert 'set rstTX to ""' in script  # missing reports → empty (cleared)


class FakeProc:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def test_push_via_applescript_success() -> None:
    captured: dict[str, list[str]] = {}

    def runner(cmd, *, capture_output, text, timeout):
        captured["cmd"] = cmd
        return FakeProc(0)

    assert push_via_applescript([REC], runner=runner) == [REC]
    assert captured["cmd"][0] == "/usr/bin/osascript"
    assert "logQSO" in captured["cmd"][2]


def test_push_via_applescript_batches() -> None:
    calls: list[list[str]] = []

    def runner(cmd, *, capture_output, text, timeout):
        calls.append(cmd)
        return FakeProc(0)

    records = [REC] * 20
    pushed = push_via_applescript(records, runner=runner, batch_size=15)
    assert len(pushed) == 20
    assert len(calls) == 2  # 15 + 5


def test_push_via_applescript_failure_and_empty() -> None:
    def fail_runner(cmd, *, capture_output, text, timeout):
        return FakeProc(1, "error: boom")

    assert push_via_applescript([REC], runner=fail_runner) == []
    assert push_via_applescript([], runner=fail_runner) == []  # nothing to do


def test_push_via_applescript_handles_runner_exception() -> None:
    def boom(cmd, *, capture_output, text, timeout):
        raise OSError("osascript missing")

    assert push_via_applescript([REC], runner=boom) == []
