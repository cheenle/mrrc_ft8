"""wsjt_udp: WSJT-X UDP message construction + send (never real packets)."""

from __future__ import annotations

from rumlog_sync.wsjt_udp import build_heartbeat, build_qso_logged, send_payload


def test_build_heartbeat_format() -> None:
    payload = build_heartbeat("MRRC-FT8-SYNC", dial_freq_hz=14_074_000)
    text = payload.decode("ascii")
    assert text.startswith("<MessageType:9>HEARTBEAT")
    assert "<Id:13>MRRC-FT8-SYNC" in text
    assert "<DialFrequency:11>14074000" in text
    assert payload.endswith(b"\x00")


def test_build_qso_logged_format() -> None:
    fields = {
        "CALL": "TL8GD",
        "GRIDSQUARE": "",
        "MODE": "FT8",
        "BAND": "20m",
        "FREQ": "14.074684",
        "RST_SENT": "-12",
        "RST_RCVD": "-12",
        "QSO_DATE": "20260810",
        "TIME_ON": "232300",
        "TIME_OFF": "232329",
        "STATION_CALLSIGN": "BG1SB",
    }
    payload = build_qso_logged(fields)
    text = payload.decode("ascii")
    assert text.startswith("<MessageType:10>QSO_LOGGED")
    assert "<CALL:5>TL8GD" in text
    assert "<EOR>" in text
    assert payload.endswith(b"\x00")
    # ADIF length prefix counts the record without <EOR> (spec §6).
    adif_body = text.split("<ADIF:", 1)[1].split(">", 1)[1].split("<EOR>", 1)[0]
    assert len(adif_body) == 162
    assert f"<ADIF:{len(adif_body)}>" in text


def test_send_payload_uses_injected_socket() -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeSocket:
        def sendto(self, data: bytes, addr: tuple[str, int]) -> int:
            sent.append((data, addr))
            return len(data)

        def close(self) -> None:
            pass

    send_payload(b"hello\x00", "127.0.0.1", 2237, sock=FakeSocket())
    assert sent == [(b"hello\x00", ("127.0.0.1", 2237))]
