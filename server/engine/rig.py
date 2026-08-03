"""Async rigctld TCP client — the only Hamlib speaker in the application.

AD-008 and §11.4: ``rigctld`` owns the CAT serial device; this client is the
application's sole radio path and never touches serial itself.  It speaks the
rigctld short-command protocol over loopback TCP, serializes commands through
one lock, fails closed on RPRT errors/timeouts/protocol garbage, and
transparently reconnects on the next command after a dropped session so the
safety controller can re-verify RX health before any new TX (§15.3/§15.5).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger("mrrc-ft8.rig")

DEFAULT_PORT = 4532
DEFAULT_TIMEOUT_SECONDS = 2.0
_MAX_REPLY_LINE = 256
_MODE_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_LEVEL_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
MIN_FREQUENCY_HZ = 1_000
MAX_FREQUENCY_HZ = 9_999_999_999
MAX_PASSBAND_HZ = 100_000

# ── FT-710 filter width (hamlib 4.6.2 workaround) ────────────────────
# Hamlib 4.6.2's FT-710 backend (Yaesu newcat) is broken in BOTH
# directions, so the raw CAT ``SH`` command goes through rigctld's
# ``\send_raw`` pass-through (AD-008 preserved: rigctld stays the sole
# serial owner):
#
# * SET: ``newcat_set_rx_bandwidth`` formats the command as ``SH0NN;``
#   instead of ``SH00NN;`` for the FT-710 (it is missing from the
#   4-digit branch), so ``M <mode> <width>`` reports RPRT 0 but the rig
#   ignores the malformed frame — the width never changes.
# * GET: ``newcat_get_rx_bandwidth`` has no FT-710 branch; the index
#   falls through to the FT-450/FT-9000 bucketing (index <16 → narrow,
#   16 → normal, >16 → wide), so ``m`` reports 2400 Hz (index 14) as
#   1800 Hz.  The "reverts after 1-3 s" seen on the wire is just the
#   500 ms hamlib set-cache expiring and exposing this misread.
#
# Index = position in hamlib's ftdx101_ssb_widths.widths[] array (the
# FT-710 reuses it); ``SH00<idx>;`` sets it, ``SH0;`` reads it back.
_FILTER_WIDTH_INDEX = {1800: 9, 2400: 14, 3000: 20}
_SSB_WIDTH_TABLE_HZ = (
    0, 300, 400, 600, 850, 1100, 1200, 1500, 1650, 1800,
    1950, 2100, 2200, 2300, 2400, 2500, 2600, 2700, 2800,
    2900, 3000, 3200, 3500, 4000,
)
_SH_REPLY_RE = re.compile(r"^SH(\d{4});")


class RigError(Exception):
    """One rigctld interaction failed closed."""

    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str, rprt: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.rprt = rprt


class RigClient:
    """Minimal async rigctld client (frequency, mode, PTT).

    ``connector`` defaults to :func:`asyncio.open_connection` and is
    injectable for tests; it must return ``(reader, writer)``.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        connector: Any = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("rig timeout must be positive")
        self._host = host
        self._port = port
        self._timeout = timeout
        self._connector = connector or asyncio.open_connection
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._unread: list[bytes] = []

    @property
    def connected(self) -> bool:
        """Whether a live session is believed to exist."""

        return self._writer is not None

    async def connect(self) -> None:
        """Open the rigctld session; idempotent when already connected."""

        async with self._lock:
            await self._ensure_connected_locked()

    async def close(self) -> None:
        """Close the session; idempotent."""

        async with self._lock:
            await self._drop_locked()

    async def get_frequency(self) -> int:
        """Return the current VFO frequency in Hz."""

        lines = await self._query("f", 1, skip_stale_rprt=True)
        return self._parse_int(lines[0], "frequency")

    async def set_frequency(self, frequency_hz: int) -> None:
        """Set the VFO frequency in Hz."""

        if (
            type(frequency_hz) is not int
            or not MIN_FREQUENCY_HZ <= frequency_hz <= MAX_FREQUENCY_HZ
        ):
            raise ValueError("frequency is outside the supported Hz range")
        await self._set(f"F {frequency_hz}")

    async def get_ptt(self) -> bool:
        """Return True while the rig is transmitting."""

        lines = await self._query("t", 1, skip_stale_rprt=True)
        return self._parse_int(lines[0], "PTT state") != 0

    async def set_ptt(self, transmit: bool) -> None:
        """Key or unkey the rig. Only safety code may call this (§15.1)."""

        await self._set(f"T {1 if transmit else 0}")

    async def get_mode(self) -> tuple[str, int]:
        """Return the current (mode, passband Hz) pair.

        rigctld replies to ``m`` with ``<MODE>.<passband>.`` on one line
        (FT-710 confirms: ``USB.1800.``); some builds/rigs split it across
        two lines.  Both shapes are accepted.
        """

        lines = await self._query("m", 1, skip_stale_rprt=True)
        first = lines[0].strip()
        if "." in first:
            # Single line ``MODE.passband.`` (rigctld dotted format).
            parts = first.split(".")
            mode = parts[0]
            passband_text = parts[1] if len(parts) > 1 else ""
        else:
            # Two-line shape: ``MODE`` then ``passband``.
            mode = first
            rest = await self._readline_locked()
            passband_text = rest.strip()
        if not _MODE_RE.match(mode):
            raise RigError(
                "protocol", f"rig returned an invalid mode token: {mode!r}"
            )
        return mode, self._parse_int(passband_text, "passband")

    async def set_mode(self, mode: str, passband_hz: int) -> None:
        """Set mode and passband (e.g. USB/2400 for FT8)."""

        if not _MODE_RE.match(mode):
            raise ValueError("mode must be an uppercase Hamlib token")
        if type(passband_hz) is not int or not 0 <= passband_hz <= MAX_PASSBAND_HZ:
            raise ValueError("passband is outside the supported Hz range")
        await self._set(f"M {mode} {passband_hz}")

    # ---- rigctld level/function access (best effort per rig) -----------

    async def get_level(self, level: str) -> float:
        """Read one rig level (e.g. ``ATT``, ``RF``, ``PREAMP``, ``AGC``).

        ``RPRT -11`` (unsupported on this rig) surfaces as :class:`RigError`
        with ``code == "rig_unsupported"``; callers may degrade gracefully.
        """

        if not _LEVEL_RE.match(level):
            raise ValueError("level must be an uppercase Hamlib token")
        try:
            # FT-710 never answers ``L <name>``; cap the read so the rig
            # lock is released quickly instead of stalling for 2 s per
            # level (a drawer open would stall rig_poll / mode switches).
            lines = await self._query(f"L {level}", 1, timeout=0.5)
        except (asyncio.TimeoutError, RigError) as exc:
            if isinstance(exc, RigError) and exc.rprt == -11:
                raise RigError(
                    "rig_unsupported", f"rig does not expose level {level}", rprt=-11
                ) from None
            # Timeout or any other rig error: treat as unsupported query
            # rather than stalling the rig lock.  (The write path
            # ``l <level> <value>`` still works on FT-710.)
            raise RigError(
                "rig_unsupported", f"rig does not expose level {level}", rprt=-11
            ) from None
        return float(lines[0])

    async def set_level(self, level: str, value: float) -> None:
        """Write one rig level; unsupported levels raise ``rig_unsupported``.

        NB: rigctld replies to ``l <level> <value>`` with the *new value*
        (e.g. ``6.``) on rigs like the FT-710, not ``RPRT 0`` — so this
        path tolerates both reply styles (``M``/``F``/``T`` keep using
        :meth:`_set` which requires RPRT).
        """

        if not _LEVEL_RE.match(level):
            raise ValueError("level must be an uppercase Hamlib token")
        if type(value) not in (int, float):
            raise ValueError("level value must be numeric")
        async with self._lock:
            await self._ensure_connected_locked()
            await self._write_locked(f"l {level} {value}")
            line = await self._readline_locked()
            if line.startswith("RPRT"):
                try:
                    self._raise_rprt(line)
                except RigError as exc:
                    if exc.rprt == -11:
                        raise RigError(
                            "rig_unsupported",
                            f"rig does not expose level {level}",
                            rprt=-11,
                        ) from None
                    raise
                return
            # Otherwise the rig echoed the new value back (float like ``6.``);
            # accept it as success.  Some rigs (FT-710) then dump their full
            # capability text and close with ``RPRT 0`` — drain until RPRT so
            # the trailing lines never leak into the next command.
            try:
                float(line)
            except ValueError:
                raise RigError(
                    "protocol", f"level set did not return a value or RPRT: {line!r}"
                ) from None
            await self._drain_until_rprt()

    async def set_filter_width(self, hz: int) -> None:
        """Set the FT-710 SSB filter width via raw CAT ``SH00<NN>;``.

        hamlib 4.6.2 mis-frames the command inside ``M <mode> <pb>``
        (sends ``SH0NN;``), so the width is written as a correctly framed
        raw command through rigctld's ``\\send_raw`` pass-through — AD-008
        preserved.  Argument 1 of ``\\send_raw`` is the *expected reply*
        spec (``0`` = no reply expected), not a VFO.  rigctld answers
        ``No answer`` on success; an ``RPRT`` line means it failed.
        """

        idx = _FILTER_WIDTH_INDEX.get(hz)
        if idx is None:
            raise ValueError(f"unsupported filter width: {hz} Hz")
        async with self._lock:
            await self._ensure_connected_locked()
            await self._write_locked(f"\\send_raw 0 SH{idx:04d};")
            line = await self._readline_locked()
            if line.startswith("RPRT"):
                self._raise_rprt(line)

    async def get_filter_width(self) -> int:
        """Read the FT-710's actual SSB filter width via raw CAT ``SH0;``.

        hamlib 4.6.2's ``m`` passband is unreliable on the FT-710 (index
        14 / 2400 Hz reads back as 1800 Hz), so the SH register is read
        through rigctld's ``\\send_raw`` and mapped with hamlib's
        ``ftdx101_ssb_widths`` table.  ``\\send_raw ;`` reads the rig's
        reply up to ``;`` and returns it newline-terminated.
        """

        async with self._lock:
            await self._ensure_connected_locked()
            await self._write_locked("\\send_raw ; SH0;")
            line = await self._readline_locked()
        match = _SH_REPLY_RE.match(line)
        if match is None:
            raise RigError(
                "protocol", f"rig returned an invalid width reply: {line!r}"
            )
        idx = int(match.group(1))
        if idx >= len(_SSB_WIDTH_TABLE_HZ):
            raise RigError(
                "protocol", f"rig returned an unknown width index: {idx}"
            )
        return _SSB_WIDTH_TABLE_HZ[idx]

    # ---- internals ------------------------------------------------------

    async def _drain_until_rprt(self) -> None:
        """Drain rigctld output until an ``RPRT`` line (or timeout).

        Some rigs (FT-710) follow a level-set value echo with their full
        capability dump ending in ``RPRT 0``; without draining, those lines
        leak into the next command's reply and corrupt the protocol stream.
        """

        while True:
            try:
                line = await self._readline_locked()
            except RigError:
                return  # connection dropped — next command reconnects
            if line.startswith("RPRT"):
                self._raise_rprt(line)
                return

    async def _query(
        self,
        payload: str,
        reply_lines: int,
        *,
        skip_stale_rprt: bool = False,
        timeout: float | None = None,
    ) -> list[str]:
        async with self._lock:
            await self._ensure_connected_locked()
            await self._write_locked(payload)
            lines: list[str] = []
            for _ in range(reply_lines):
                line = await self._readline_locked(timeout=timeout)
                if skip_stale_rprt:
                    # A stale ``RPRT`` from a previous command (e.g. the
                    # delayed ``RPRT -11`` of an unsupported level query on
                    # FT-710) can land on this session before our reply.
                    # Skip up to a few RPRT lines and read the real payload.
                    for _ in range(8):
                        if not line.startswith("RPRT"):
                            break
                        line = await self._readline_locked(timeout=timeout)
                lines.append(line)
            for line in lines:
                if line.startswith("RPRT"):
                    self._raise_rprt(line)
            return lines

    async def _set(self, payload: str) -> None:
        async with self._lock:
            await self._ensure_connected_locked()
            await self._write_locked(payload)
            line = await self._readline_locked()
            if not line.startswith("RPRT"):
                raise RigError("protocol", "set command did not return RPRT")
            self._raise_rprt(line)
            # FT-710's rigctld appends a blank line after set replies
            # (``RPRT 0\n\n``); ``_readline_locked`` skips blanks so the
            # stream is clean for the next command.
            return

    async def _ensure_connected_locked(self) -> None:
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                self._connector(self._host, self._port), timeout=self._timeout
            )
        except (OSError, asyncio.TimeoutError):
            self._reader = self._writer = None
            raise RigError(
                "rig_unreachable", "rigctld connection could not be established"
            ) from None
        # A fresh session may still have the previous session's trailing
        # output buffered (e.g. a killed client mid-reply).  Drain any
        # residual lines so the first command never reads stale data.
        await self._drain_stale_input()

    async def _drain_stale_input(self) -> None:
        """Consume leftover bytes on a freshly-opened session.

        rigctld keeps one connection per client; when a client dies mid-
        command (restart, timeout drop), its pending reply can linger on
        the new session.  Reading it here keeps the first real command
        clean.  Timeouts are expected (usually nothing is buffered).
        """

        assert self._reader is not None
        self._unread.clear()
        for _ in range(64):
            try:
                raw = await asyncio.wait_for(
                    self._reader.readline(), timeout=0.05
                )
            except asyncio.TimeoutError:
                return  # stream quiet — clean start
            except (OSError, RuntimeError):
                return
            if raw == b"":
                await self._drop_locked()
                return
            if not raw.strip():
                continue
            # Real payload bytes before our first command: treat as stale
            # and discard (we never sent anything on this session yet).
            if len(raw) > _MAX_REPLY_LINE:
                await self._drop_locked()
                return

    async def _write_locked(self, payload: str) -> None:
        assert self._writer is not None
        try:
            log.debug("rig >>> %r", payload)
            self._writer.write(payload.encode("ascii") + b"\n")
            await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
        except (OSError, asyncio.TimeoutError):
            await self._drop_locked()
            raise RigError("rig_disconnected", "rigctld write failed") from None

    async def _readline_locked(self, *, timeout: float | None = None) -> str:
        assert self._reader is not None
        limit = self._timeout if timeout is None else timeout
        while True:
            if self._unread:
                raw = self._unread.pop(0)
            else:
                try:
                    raw = await asyncio.wait_for(
                        self._reader.readline(), timeout=limit
                    )
                except asyncio.TimeoutError:
                    await self._drop_locked()
                    raise RigError("rig_timeout", "rigctld reply timed out") from None
                except OSError:
                    await self._drop_locked()
                    raise RigError("rig_disconnected", "rigctld read failed") from None
                if raw == b"":
                    await self._drop_locked()
                    raise RigError("rig_disconnected", "rigctld closed the session")
                if len(raw) > _MAX_REPLY_LINE:
                    await self._drop_locked()
                    raise RigError("protocol", "rigctld reply line is oversize")
            try:
                text = raw.decode("ascii", errors="strict").rstrip("\r\n")
            except UnicodeDecodeError:
                await self._drop_locked()
                raise RigError("protocol", "rigctld reply was not ASCII") from None
            # FT-710's rigctld interleaves blank lines after set replies;
            # skip them so every reply is a real payload line.
            if text:
                log.debug("rig <<< %r", text)
                return text

    async def _drop_locked(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    @staticmethod
    def _raise_rprt(line: str) -> None:
        parts = line.split()
        if len(parts) != 2:
            raise RigError("protocol", "malformed RPRT reply")
        try:
            code = int(parts[1])
        except ValueError:
            raise RigError("protocol", "malformed RPRT code") from None
        if code != 0:
            raise RigError("rig_rprt", "rigctld rejected the command", rprt=code)

    @staticmethod
    def _parse_int(text: str, what: str) -> int:
        try:
            return int(text)
        except ValueError:
            raise RigError(
                "protocol", f"rig returned an invalid {what}: {text!r}"
            ) from None
