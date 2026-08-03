from __future__ import annotations

import asyncio
import re

import pytest

from server.engine.rig import RigClient, RigError


class FakeRigctld:
    """Minimal rigctld short-protocol server with failure injection."""

    def __init__(self) -> None:
        self.frequency = 14_074_000
        self.ptt = 0
        self.mode = ("USB", 2_400)
        # True rig SH-register width index (hamlib's ``m`` passband above may
        # disagree with it — that mismatch is exactly the FT-710 bug).
        self.width_index = 9  # 1800 Hz power-on default
        self.levels: dict[str, float] = {"ATT": 0.0, "PREAMP": 0.0, "RF": 50.0, "AGC": 30.0}
        self.rprt_error = 0          # when nonzero, every set command fails
        self.silent = False          # never reply (timeout injection)
        self.garbage = False         # reply with non-protocol bytes
        self.drop_after: int | None = None  # close session after N commands
        self.dump_caps_on_level: str | None = None  # level whose set triggers caps dump
        self.stale_rprt_once: int | None = None  # send one stale RPRT before this command count
        self.commands: list[str] = []
        self.sessions = 0
        self._server: asyncio.AbstractServer | None = None
        self._stale_sent = False

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
                # Simulate a delayed RPRT from a previous command landing on
                # a fresh session before the real reply (FT-710 level query).
                if self.stale_rprt_once is not None and not self._stale_sent:
                    self._stale_sent = True
                    writer.write(b"RPRT -11\n")
                    await writer.drain()
                reply = self._reply(command)
                for line in reply.split("\n"):
                    writer.write(line.encode("ascii") + b"\n")
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
            # rigctld dotted format: ``MODE.passband.`` on one line.
            return f"{self.mode[0]}.{self.mode[1]}."
        if command.startswith("F "):
            if self.rprt_error:
                return f"RPRT {self.rprt_error}"
            self.frequency = int(command.split()[1])
            return "RPRT 0\n"  # FT-710 appends a trailing blank after sets
        if command.startswith("T "):
            if self.rprt_error:
                return f"RPRT {self.rprt_error}"
            self.ptt = int(command.split()[1])
            return "RPRT 0\n"
        if command.startswith("M "):
            if self.rprt_error:
                return f"RPRT {self.rprt_error}"
            _, mode, passband = command.split()
            self.mode = (mode, int(passband))
            return "RPRT 0\n"
        if command.startswith("L "):
            name = command.split()[1]
            if name not in self.levels:
                return "RPRT -11"
            return str(self.levels[name])
        if command.startswith("l "):
            if self.rprt_error:
                return f"RPRT {self.rprt_error}"
            _, name, value = command.split()
            if name not in self.levels:
                return "RPRT -11"
            self.levels[name] = float(value)
            if name == self.dump_caps_on_level:
                # FT-710 behaviour: value echo + full caps dump + closing RPRT.
                return "0.\nCaps dump for model: 1049\nModel name:\tFT-710\nRPRT 0"
            # rigctld echoes the new value (FT-710 style), not RPRT.
            return f"{float(value):g}."
        if command.startswith("\\send_raw "):
            if self.rprt_error:
                return f"RPRT {self.rprt_error}"
            parts = command.split(" ", 2)
            if len(parts) != 3:
                return "RPRT -1"
            spec, payload = parts[1], parts[2]
            if spec == "0":
                # Raw write, no rig reply.  Like the real FT-710, only a
                # correctly framed ``SH00NN;`` (4 digits) changes the width —
                # a 2-digit ``SHNN;`` is ignored by the rig.
                frame = re.fullmatch(r"SH(\d{4});", payload)
                if frame:
                    self.width_index = int(frame.group(1))
                return "No answer"
            if spec == ";" and payload == "SH0;":
                # Raw write + read-until-';': the rig's SH register.
                return f"SH{self.width_index:04d};"
            return "No answer"
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


def test_level_round_trip_and_unsupported(rig: FakeRigctld) -> None:
    """ATT/AGC/PREAMP/RF level read/write; unsupported level -> rig_unsupported."""

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            assert await client.get_level("ATT") == 0.0
            await client.set_level("ATT", 1.0)
            assert await client.get_level("ATT") == 1.0
            assert rig.levels["ATT"] == 1.0
            # Unknown level: hamlib replies RPRT -11 (not supported on this rig).
            with pytest.raises(RigError) as exc:
                await client.get_level("SQL")
            assert exc.value.code == "rig_unsupported"
        finally:
            await client.close()
            await rig.stop()

    run(main())
    assert rig.commands[:3] == ["L ATT", "l ATT 1.0", "L ATT"]


def test_level_set_drains_caps_dump(rig: FakeRigctld) -> None:
    """FT-710 echoes a value then dumps full caps + RPRT; trailing lines must
    not leak into the next command's reply."""

    rig.dump_caps_on_level = "PREAMP"

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=2.0)
        try:
            await client.set_level("PREAMP", 10)
            assert rig.levels["PREAMP"] == 10.0
            # Next command must see a clean stream (no caps-dump residue).
            assert await client.get_level("ATT") == 0.0
            await client.set_level("AGC", 6)
            assert rig.levels["AGC"] == 6.0
        finally:
            await client.close()
            await rig.stop()

    run(main())


def test_query_skips_stale_rprt_from_previous_command(rig: FakeRigctld) -> None:
    """FT-710 delays the RPRT -11 of an unsupported level query; it can land
    on the next session before our reply.  ``m``/``f`` queries must skip it."""

    rig.stale_rprt_once = 1

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=2.0)
        try:
            mode, passband = await client.get_mode()
            assert mode == "USB"
            assert passband == 2400
            freq = await client.get_frequency()
            assert freq == 14_074_000
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


def test_filter_width_set_sends_correctly_framed_sh(rig: FakeRigctld) -> None:
    """FT-710 width set goes through rigctld ``\\send_raw`` as ``SH00<NN>;``.

    hamlib 4.6.2's ``M <mode> <pb>`` mis-frames the command (``SH0NN;``) so
    the rig ignores it; the 4-digit frame is the only one the rig accepts.
    """

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            await client.set_filter_width(2_400)
            assert rig.width_index == 14
            await client.set_filter_width(1_800)
            assert rig.width_index == 9
            await client.set_filter_width(3_000)
            assert rig.width_index == 20
        finally:
            await client.close()
            await rig.stop()

    run(main())
    assert "\\send_raw 0 SH0014;" in rig.commands
    assert "\\send_raw 0 SH0009;" in rig.commands
    assert "\\send_raw 0 SH0020;" in rig.commands


def test_filter_width_set_rejects_unknown_width(rig: FakeRigctld) -> None:
    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            with pytest.raises(ValueError):
                await client.set_filter_width(2_300)
        finally:
            await client.close()
            await rig.stop()

    run(main())
    assert rig.commands == []  # nothing reached the wire


def test_filter_width_set_surfaces_rprt_error(rig: FakeRigctld) -> None:
    rig.rprt_error = -1

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            with pytest.raises(RigError) as caught:
                await client.set_filter_width(2_400)
            assert caught.value.code == "rig_rprt"
        finally:
            await client.close()
            await rig.stop()

    run(main())


def test_filter_width_get_reads_sh_register(rig: FakeRigctld) -> None:
    """The true width comes from the rig's SH register, not hamlib's ``m``
    (which misreports index 14 / 2400 Hz as 1800 Hz on hamlib 4.6.2)."""

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            rig.width_index = 14
            assert await client.get_filter_width() == 2_400
            rig.width_index = 9
            assert await client.get_filter_width() == 1_800
            rig.width_index = 20
            assert await client.get_filter_width() == 3_000
        finally:
            await client.close()
            await rig.stop()

    run(main())
    assert "\\send_raw ; SH0;" in rig.commands


def test_filter_width_get_rejects_bad_reply(rig: FakeRigctld) -> None:
    rig.garbage = True

    async def main() -> None:
        port = await rig.start()
        client = RigClient(port=port, timeout=1.0)
        try:
            with pytest.raises(RigError) as caught:
                await client.get_filter_width()
            assert caught.value.code == "protocol"
        finally:
            await client.close()
            await rig.stop()

    run(main())


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
