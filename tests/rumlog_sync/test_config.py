"""rumlog_sync.config: defaults, JSON override, validation."""

from __future__ import annotations

import json

import pytest

from rumlog_sync.config import DEFAULT_CONFIG, load_config, validate_config

VALID = {
    "ft8_db": "data/mrrc-ft8.db",
    "rumlog_db": "/tmp/CoreQsoModel_1.sqlite",
    "my_call": "BG1SB",
    "my_grid": "ON80DA",
    "udp_host": "127.0.0.1",
    "udp_port": 2237,
    "udp_id": "MRRC-FT8-SYNC",
    "confirm_retries": 3,
    "lock_path": "data/rumlog-sync.lock",
}


def test_defaults_are_self_consistent() -> None:
    cfg = validate_config(dict(DEFAULT_CONFIG))
    assert cfg["udp_port"] == 2237
    assert cfg["confirm_retries"] >= 1


def test_load_config_returns_defaults_when_file_missing(tmp_path) -> None:
    cfg = load_config(tmp_path / "nope.json")
    assert cfg["udp_port"] == 2237
    assert cfg["my_call"] == "BG1SB"


def test_load_config_merges_json_over_defaults(tmp_path) -> None:
    path = tmp_path / "rumlog-sync.json"
    path.write_text(json.dumps({"udp_port": 9999, "my_call": "BA7ABC"}))
    cfg = load_config(path)
    assert cfg["udp_port"] == 9999
    assert cfg["my_call"] == "BA7ABC"
    assert cfg["udp_host"] == "127.0.0.1"  # untouched default


def test_load_config_rejects_bad_json(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_config(path)


def test_validate_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        validate_config({"bogus_key": 1})


def test_validate_config_enforces_types_and_ranges() -> None:
    bad = dict(VALID, udp_port=0)
    with pytest.raises(ValueError):
        validate_config(bad)
    bad2 = dict(VALID, confirm_retries=0)
    with pytest.raises(ValueError):
        validate_config(bad2)
