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
import re
from typing import Any

DEFAULT_PORT = 4532
DEFAULT_TIMEOUT_SECONDS = 2.0
_MAX_REPLY_LINE = 256
_MODE_RE = re.compile(r"^[A-Z0-9]{1,16}$")
MIN_FREQUENCY_HZ = 1_000
MAX_FREQUENCY_HZ = 9_999_999_999
MAX_PASSBAND_HZ = 100_000


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

        lines = await self._query("f", 1)
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

        lines = await self._query("t", 1)
        return self._parse_int(lines[0], "PTT state") != 0

    async def set_ptt(self, transmit: bool) -> None:
        """Key or unkey the rig. Only safety code may call this (§15.1)."""

        await self._set(f"T {1 if transmit else 0}")

    async def get_mode(self) -> tuple[str, int]:
        """Return the current (mode, passband Hz) pair."""

        lines = await self._query("m", 2)
        mode = lines[0]
        if not _MODE_RE.match(mode):
            raise RigError("protocol", "rig returned an invalid mode token")
        return mode, self._parse_int(lines[1], "passband")

    async def set_mode(self, mode: str, passband_hz: int) -> None:
        """Set mode and passband (e.g. USB/2400 for FT8)."""

        if not _MODE_RE.match(mode):
            raise ValueError("mode must be an uppercase Hamlib token")
        if type(passband_hz) is not int or not 0 <= passband_hz <= MAX_PASSBAND_HZ:
            raise ValueError("passband is outside the supported Hz range")
        await self._set(f"M {mode} {passband_hz}")

    # ---- internals ------------------------------------------------------

    async def _query(self, payload: str, reply_lines: int) -> list[str]:
        async with self._lock:
            await self._ensure_connected_locked()
            await self._write_locked(payload)
            lines = [await self._readline_locked() for _ in range(reply_lines)]
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

    async def _write_locked(self, payload: str) -> None:
        assert self._writer is not None
        try:
            self._writer.write(payload.encode("ascii") + b"\n")
            await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)
        except (OSError, asyncio.TimeoutError):
            await self._drop_locked()
            raise RigError("rig_disconnected", "rigctld write failed") from None

    async def _readline_locked(self) -> str:
        assert self._reader is not None
        try:
            raw = await asyncio.wait_for(
                self._reader.readline(), timeout=self._timeout
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
            return raw.decode("ascii", errors="strict").rstrip("\r\n")
        except UnicodeDecodeError:
            await self._drop_locked()
            raise RigError("protocol", "rigctld reply was not ASCII") from None

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
            raise RigError("protocol", f"rig returned an invalid {what}") from None
