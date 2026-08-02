from __future__ import annotations

import asyncio

import pytest

from server.engine.rig import RigClient, RigError


class FakeRigctld:
    """Minimal rigctld short-protocol server with failure injection."""

    def __init__(self) -> None:
        self.frequency = 14_074_000
        self.ptt = 0
        self.mode = ("USB", 2_400)
        self.rprt_error = 0          # when nonzero, every set command fails
        self.silent = False          # never reply (timeout injection)
        self.garbage = False         # reply with non-protocol bytes
        self.drop_after: int | None = None  # close session after N commands
        self.commands: list[str] = []
        self.sessions = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]  # type: ignore[index]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.sessions += 1
        handled = 0
        try:
            while True:
                raw = await reader.readline()
                if raw == b"":
                    return
                command = raw.decode("ascii", errors="replace").strip()
                self.commands.append(command)
                handled += 1
                if self.silent:
                    continue
                if self.garbage:
                    writer.write(b"\xff\xfe not a reply\r\n")
                    await writer.drain()
                    continue
                writer.write(self._reply(command).encode("ascii") + b"\n")
                await writer.drain()
                if self.drop_after is not None and handled >= self.drop_after:
                    return
        finally:
            writer.close()

    def _reply(self, command: str) -> str:
        if command == "f":
            return str(self.frequency)
        if command == "t":
            return str(self.ptt)
        if command == "m":
            return f"{self.mode[0]}\n{self.mode[1]}"
        if command.startswith("F "):
            if self.rprt_error:
                return f"RPRT {self.rprt_error}"
            self.frequency = int(command.split()[1])
            return "RPRT 0"
        if command.startswith("T "):
            if self.rprt_error:
                return f"RPRT {self.rprt_error}"
            self.ptt = int(command.split()[1])
            return "RPRT 0"
        if command.startswith("M "):
            if self.rprt_error:
                return f"RPRT {self.rprt_error}"
            _, mode, passband = command.split()
            self.mode = (mode, int(passband))
            return "RPRT 0"
        return "RPRT -1"


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture()
def rig() -> FakeRigctld:
    return FakeRigctld()


def test_frequency_round_trip(rig: FakeRigctld) -> None:
    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            assert not client.connected
            assert await client.get_frequency() == 14_074_000
            assert client.connected
            await client.set_frequency(7_074_000)
            assert await client.get_frequency() == 7_074_000
        finally:
            await client.close()
            await rig.stop()
        assert not client.connected

    run(main())
    assert rig.commands[:4] == ["f", "F 7074000", "f"]


def test_ptt_and_mode(rig: FakeRigctld) -> None:
    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            assert await client.get_ptt() is False
            await client.set_ptt(True)
            assert await client.get_ptt() is True
            await client.set_ptt(False)
            assert await client.get_ptt() is False
            await client.set_mode("PKTUSB", 3_000)
            assert await client.get_mode() == ("PKTUSB", 3_000)
        finally:
            await client.close()
            await rig.stop()

    run(main())


def test_rprt_error_raises_with_code(rig: FakeRigctld) -> None:
    rig.rprt_error = -11

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            with pytest.raises(RigError) as caught:
                await client.set_ptt(True)
            assert caught.value.code == "rig_rprt"
            assert caught.value.rprt == -11
        finally:
            await client.close()
            await rig.stop()

    run(main())
    assert rig.ptt == 0  # the rig state was untouched


def test_timeout_disconnects_and_recovers(rig: FakeRigctld) -> None:
    rig.silent = True

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=0.2)
        try:
            with pytest.raises(RigError) as caught:
                await client.get_frequency()
            assert caught.value.code == "rig_timeout"
            assert not client.connected

            rig.silent = False  # rigctld healthy again: transparent reconnect
            assert await client.get_frequency() == 14_074_000
            assert client.connected
        finally:
            await client.close()
            await rig.stop()

    run(main())
    assert rig.sessions == 2


def test_dropped_session_reconnects_on_next_command(rig: FakeRigctld) -> None:
    rig.drop_after = 1

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            assert await client.get_frequency() == 14_074_000
            with pytest.raises(RigError) as caught:
                await client.get_ptt()
            assert caught.value.code == "rig_disconnected"

            rig.drop_after = None
            assert await client.get_ptt() is False
        finally:
            await client.close()
            await rig.stop()

    run(main())
    # Session 1 served f then dropped; the failed t reused its corpse and the
    # retry opened session 2.
    assert rig.sessions == 2


def test_garbage_reply_is_protocol_error(rig: FakeRigctld) -> None:
    rig.garbage = True

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            with pytest.raises(RigError) as caught:
                await client.get_frequency()
            assert caught.value.code == "protocol"
            assert not client.connected
        finally:
            await client.close()
            await rig.stop()

    run(main())


def test_unreachable_rig_fails_closed(rig: FakeRigctld) -> None:
    async def main() -> None:
        port = await rig.start()
        await rig.stop()  # nothing listens now
        client = RigClient(port=port, timeout=0.3)
        with pytest.raises(RigError) as caught:
            await client.get_frequency()
        assert caught.value.code == "rig_unreachable"
        assert not client.connected

    run(main())


def test_invalid_inputs_are_rejected_before_io(rig: FakeRigctld) -> None:
    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            with pytest.raises(ValueError):
                await client.set_frequency(0)
            with pytest.raises(ValueError):
                await client.set_frequency(10_000_000_000)
            with pytest.raises(ValueError):
                await client.set_mode("usb", 2_400)
            with pytest.raises(ValueError):
                await client.set_mode("USB", -1)
            with pytest.raises(ValueError):
                RigClient(port=port, timeout=0.0)
        finally:
            await client.close()
            await rig.stop()

    run(main())
    assert rig.commands == []  # no garbage reached the wire


def test_commands_are_serialized_through_the_lock(rig: FakeRigctld) -> None:
    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            results = await asyncio.gather(
                client.get_frequency(), client.get_ptt(), client.get_mode()
            )
            assert results == [14_074_000, False, ("USB", 2_400)]
        finally:
            await client.close()
            await rig.stop()

    run(main())
    # One interleaving-free command stream on a single session.
    assert rig.sessions == 1
    assert rig.commands == ["f", "t", "m"]


def test_close_is_idempotent(rig: FakeRigctld) -> None:
    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        await client.connect()
        await client.close()
        await client.close()
        assert not client.connected
        await rig.stop()

    run(main())
