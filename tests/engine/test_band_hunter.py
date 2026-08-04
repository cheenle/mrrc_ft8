"""Unit tests for server/engine/band_hunter.py (NFR-088)."""

import asyncio

import httpx

from server.engine.band_hunter import (
    decide_switch,
    fetch_opportunities,
    rank_bands,
)


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _band(name: str, dial: int, spots: int = 10, snr: float = -10.0, entities=()) -> dict:
    return {
        "band": name,
        "dial_freq_hz": dial,
        "nearby_spot_count": spots,
        "distinct_calls": ["X1ABC"],
        "entities": [{"name": e, "adif": i} for i, e in enumerate(entities)],
        "avg_snr": snr,
        "most_recent_ts": "2026-08-05T00:00:00Z",
    }


# --- rank_bands ---


def test_rank_bands_drops_fully_worked_bands() -> None:
    payload = {
        "ok": True,
        "bands": [
            _band("20m", 14_074_000, entities=("Japan", "USA")),
            _band("15m", 21_074_000, entities=("Japan",)),  # all worked
            _band("40m", 7_074_000, entities=("Brazil",)),
        ],
    }
    ranked = rank_bands(payload, {"Japan", "USA"})
    assert [b["band"] for b in ranked] == ["40m"]
    assert ranked[0]["new_entities"] == ["Brazil"]


def test_rank_bands_sorts_by_new_entity_count_then_spots() -> None:
    payload = {
        "ok": True,
        "bands": [
            _band("20m", 14_074_000, spots=100, entities=("Japan", "USA")),
            _band("15m", 21_074_000, spots=200, entities=("Brazil", "Canada")),
            _band("40m", 7_074_000, spots=300, entities=("Japan",)),  # all worked
        ],
    }
    ranked = rank_bands(payload, {"Japan"})
    assert [b["band"] for b in ranked] == ["15m", "20m"]
    assert ranked[0]["new_entities"] == ["Brazil", "Canada"]
    assert ranked[1]["new_entities"] == ["USA"]


def test_rank_bands_sorts_tie_by_nearby_spots() -> None:
    payload = {
        "ok": True,
        "bands": [
            _band("20m", 14_074_000, spots=50, snr=-5.0, entities=("Japan",)),
            _band("15m", 21_074_000, spots=120, snr=-12.0, entities=("Japan",)),
        ],
    }
    ranked = rank_bands(payload, {"USA"})
    assert ranked[0]["band"] == "15m"  # more nearby spots wins the tie


def test_rank_bands_bad_payload() -> None:
    assert rank_bands(None, set()) == []
    assert rank_bands({}, set()) == []
    assert rank_bands({"ok": True, "bands": []}, {"Japan"}) == []


# --- decide_switch ---


def test_decide_switch_requires_idle() -> None:
    ranked = rank_bands({"bands": [_band("20m", 14_074_000, entities=("Japan",))]}, set())
    assert (
        decide_switch(
            ranked,
            idle=False,
            current_freq_hz=7_074_000,
            seconds_since_last_switch=9999,
            cooldown_s=1200,
        )
        is None
    )


def test_decide_switch_no_candidates() -> None:
    assert (
        decide_switch([], idle=True, current_freq_hz=7_074_000,
                      seconds_since_last_switch=9999, cooldown_s=1200)
        is None
    )


def test_decide_switch_cooldown_blocks() -> None:
    ranked = rank_bands({"bands": [_band("20m", 14_074_000, entities=("Japan",))]}, set())
    assert (
        decide_switch(
            ranked, idle=True, current_freq_hz=7_074_000,
            seconds_since_last_switch=100, cooldown_s=1200,
        )
        is None
    )


def test_decide_switch_already_on_band() -> None:
    ranked = rank_bands({"bands": [_band("20m", 14_074_000, entities=("Japan",))]}, set())
    assert (
        decide_switch(
            ranked, idle=True, current_freq_hz=14_074_000,
            seconds_since_last_switch=9999, cooldown_s=1200,
        )
        is None
    )


def test_decide_switch_returns_target() -> None:
    ranked = rank_bands({"bands": [_band("20m", 14_074_000, entities=("Japan",))]}, set())
    assert (
        decide_switch(
            ranked, idle=True, current_freq_hz=7_074_000,
            seconds_since_last_switch=9999, cooldown_s=1200,
        )
        == 14_074_000
    )


def test_decide_switch_first_pass_has_no_cooldown() -> None:
    ranked = rank_bands({"bands": [_band("20m", 14_074_000, entities=("Japan",))]}, set())
    assert (
        decide_switch(
            ranked, idle=True, current_freq_hz=7_074_000,
            seconds_since_last_switch=None, cooldown_s=1200,
        )
        == 14_074_000
    )


def test_decide_switch_missing_dial_frequency() -> None:
    band = dict(_band("20m", 14_074_000, entities=("Japan",)), dial_freq_hz=None)
    ranked = rank_bands({"bands": [band]}, set())
    assert (
        decide_switch(
            ranked, idle=True, current_freq_hz=7_074_000,
            seconds_since_last_switch=9999, cooldown_s=1200,
        )
        is None
    )


# --- fetch_opportunities ---


def test_fetch_opportunities_ok() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ok": True, "bands": [{"band": "20m"}]})
    )
    payload = run(fetch_opportunities("http://test/api/band_hunt", {}, transport=transport))
    assert payload == {"ok": True, "bands": [{"band": "20m"}]}


def test_fetch_opportunities_non_ok_body() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"ok": False, "reason": "db_unavailable"})
    )
    assert (
        run(fetch_opportunities("http://test/api/band_hunt", {}, transport=transport))
        is None
    )


def test_fetch_opportunities_http_error() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    assert (
        run(fetch_opportunities("http://test/api/band_hunt", {}, transport=transport))
        is None
    )


def test_fetch_opportunities_connection_error() -> None:
    def _raise(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(_raise)
    assert (
        run(fetch_opportunities("http://test/api/band_hunt", {}, transport=transport))
        is None
    )


def test_fetch_opportunities_passes_params() -> None:
    captured: dict[str, str] = {}

    def _capture(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        return httpx.Response(200, json={"ok": True, "bands": []})

    transport = httpx.MockTransport(_capture)
    run(
        fetch_opportunities(
            "http://test/api/band_hunt",
            {"home_grid": "ON80DA", "radius_km": 1000, "window_min": 30},
            transport=transport,
        )
    )
    assert captured["home_grid"] == "ON80DA"
    assert captured["radius_km"] == "1000"
    assert captured["window_min"] == "30"
