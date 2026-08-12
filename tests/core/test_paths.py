import sys
import pytest

def test_app_root_source_tree() -> None:
    from server.core.paths import app_root
    root = app_root()
    assert (root / "server" / "main.py").exists()
    assert (root / "cty.dat").exists()


def test_app_root_frozen_uses_meipass(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/virtual/_internal", raising=False)
    from server.core.paths import app_root
    assert str(app_root()) == "/virtual/_internal"


def test_app_root_frozen_falls_back_to_exe_dir(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", "/app/App/MRRC_FT8.exe", raising=False)
    from server.core.paths import app_root
    assert str(app_root()) == "/app/App"
