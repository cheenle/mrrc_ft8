"""WSJT-X UDP protocol messages (spec §6).

RUMLogNG listens on UDP 2237 and natively parses WSJT-X messages:
``HEARTBEAT`` registers a client; ``QSO_LOGGED`` carries an ADIF record.
Every message is null-terminated.  ``send_payload`` accepts an injected
socket so tests never touch the network.
"""

from __future__ import annotations

import logging
import socket
from typing import Protocol

log = logging.getLogger(__name__)

_MESSAGE_TYPE_HEARTBEAT = "<MessageType:9>HEARTBEAT"
_MESSAGE_TYPE_QSO_LOGGED = "<MessageType:10>QSO_LOGGED"


class _DatagramSocket(Protocol):
    """Minimal UDP socket surface used by ``send_payload``."""

    def sendto(self, data: bytes, addr: tuple[str, int]) -> int: ...

    def close(self) -> None: ...


def _adif_field(name: str, value: str) -> str:
    return f"<{name}:{len(value)}>{value}"


def build_heartbeat(udp_id: str, *, dial_freq_hz: int = 14_074_000) -> bytes:
    """WSJT-X HEARTBEAT message (spec §6), null-terminated."""

    body = (
        _MESSAGE_TYPE_HEARTBEAT
        + _adif_field("Id", udp_id)
        # DialFrequency uses WSJT-X's fixed 11-digit width (UInt64), not
        # len(value) — RUMLogNG parses this exact form.
        + f"<DialFrequency:11>{dial_freq_hz}"
        # Remaining WSJT-X heartbeat fields are zero/empty in our usage.
        + "<ConfigurationName:0><TxMessage:0><TxFreq:0><DeDup:0><SubTxMessage:0>"
        + "<RxDF:0><TxDF:0><TRPeriod:0><ModulationType:0><DXCall:0><DXGrid:0>"
        + "<TxEnabled:0><Transmitting:0><Decoding:0><RxEnabled:0><FECDecoded:0>"
        + "<Watchdog:0><Submode:0><FastMode:0><SpecialOperationMode:0>"
        + "<FrequencyTolerance:0><Tolerance:0><DecoderType:0><Harmonic:0>"
        + _MESSAGE_TYPE_HEARTBEAT
    )
    return (body + "\x00").encode("ascii")


def build_qso_logged(adif_fields: dict[str, str]) -> bytes:
    """WSJT-X QSO_LOGGED message: ``<ADIF:N>record<EOR>`` + null."""

    record = "".join(_adif_field(k, v) for k, v in adif_fields.items() if v != "")
    body = _MESSAGE_TYPE_QSO_LOGGED + _adif_field("ADIF", record) + "<EOR>"
    return (body + "\x00").encode("ascii")


def send_payload(
    payload: bytes,
    host: str,
    port: int,
    *,
    sock: _DatagramSocket | None = None,
) -> None:
    """Best-effort UDP send; failures are logged, never raised to the caller."""

    close_sock = sock is None
    client = sock if sock is not None else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        client.sendto(payload, (host, port))
    except OSError:
        log.exception("UDP send to %s:%d failed", host, port)
    finally:
        if close_sock:
            client.close()
