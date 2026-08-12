from pathlib import Path
import pytest

def test_ensure_self_signed_generates_pair(tmp_path) -> None:
    from windows.ssl_bootstrap import ensure_self_signed
    pair = ensure_self_signed(tmp_path / "certs")
    assert pair is not None
    cert, key = pair
    assert cert.exists() and key.exists()
    assert cert.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert key.read_bytes().startswith(b"-----BEGIN")


def test_ensure_self_signed_reuses_existing(tmp_path) -> None:
    from windows.ssl_bootstrap import ensure_self_signed
    first = ensure_self_signed(tmp_path)
    second = ensure_self_signed(tmp_path)
    assert first == second
    assert first[0].read_bytes() == second[0].read_bytes()
