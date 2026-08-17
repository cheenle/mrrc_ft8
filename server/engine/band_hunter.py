"""Propagation-driven new-DXCC band hunter (NFR-088).

Polls the pskreporter ``/api/band_hunt`` endpoint — the *only* coupling
between this repo and the pskreporter repo (HTTP, no shared code or DB
credentials). The endpoint is the propagation gate: it only reports FT8
bands that receivers within ``radius_km`` of our grid are actively
hearing, so a switch only happens when the band is open to us.

The main.py orchestrator (a) gates on idle + the ``auto_band_hunt``
setting, (b) filters each band's entities against the local worked set
via :func:`rank_bands`, (c) tunes the rig via :func:`decide_switch`,
then the existing auto-call (NFR-087) closes the QSO. This module holds
only pure/decision logic and the HTTP fetch, so it is unit-testable
without a server or radio.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

BAND_HUNT_TIMEOUT_S = 5.0
# A dial frequency within ±50 kHz of the current one is "the same band"
# (mirrors the cockpit band selector's MATCH_HZ, band.js).
MATCH_HZ = 50_000

# pskreporter 实体名 → cty.dat 规范名（country-files）。现场 2026-08-07：
# pskreporter 用普通名 "Germany"，cty 规范名是 "Fed. Rep. of Germany"，
# rank_bands 的 worked 比对失配 → 已通联实体反复被判 new，band_hunt 白追。
# 42 个实测实体名中仅这 3 个不一致；未知名称保持原样（不误伤）。
_CTY_NAME_ALIASES: dict[str, str] = {
    "Germany": "Fed. Rep. of Germany",
    "Malaysia": "West Malaysia",  # 9M 主体；East Malaysia 为独立实体
    "Turkey": "Asiatic Turkey",   # TA 前缀主体；European Turkey 独立
}


def _canonical_entity_name(name: str) -> str:
    """Map a pskreporter entity name to the cty.dat canonical name."""

    return _CTY_NAME_ALIASES.get(name, name)


async def fetch_opportunities(
    base_url: str,
    params: dict[str, Any],
    timeout_s: float = BAND_HUNT_TIMEOUT_S,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Optional[dict[str, Any]]:
    """GET the band_hunt endpoint and return its JSON payload.

    Returns ``None`` on any failure (unreachable, non-200, malformed, or
    an ``ok: false`` body) so a DB outage on the pskreporter side never
    disturbs the FT8 server.  ``transport`` is injectable for tests
    (``httpx.MockTransport``).
    """
    kwargs: dict[str, Any] = {"timeout": timeout_s}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(base_url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception:
        return None
    if isinstance(payload, dict) and payload.get("ok") is True:  # pi-lens-ignore: no-identity-operator-on-literals
        return payload
    return None


def rank_bands(
    opportunities: Optional[dict[str, Any]], worked_entities: set[str]
) -> list[dict[str, Any]]:
    """Keep bands with at least one unworked DXCC entity, ranked.

    Pure function. ``opportunities`` is the ``/api/band_hunt`` payload
    (``{"bands": [...]}``) or ``None`` (fetch failure — returns []).
    ``worked_entities`` is the local worked set of DXCC entity names
    (e.g. ``{e.name for e in state.dxcc_cache.entities}``).

    Returns band dicts enriched with ``new_entities``, sorted by
    (new-entity count desc, nearby_spot_count desc, avg_snr desc). A band
    with no new entity is dropped entirely — it cannot yield a new DXCC.
    """
    if not isinstance(opportunities, dict):
        return []
    ranked: list[dict[str, Any]] = []
    for band in opportunities.get("bands", []):
        entities = band.get("entities", [])
        new_entities = [
            e["name"]
            for e in entities
            if isinstance(e, dict)
            and _canonical_entity_name(e.get("name", "")) not in worked_entities
        ]
        if not new_entities:
            continue
        ranked.append({**band, "new_entities": new_entities})
    ranked.sort(
        key=lambda b: (
            len(b["new_entities"]),
            _spot_count(b),
            _avg_snr(b),
        ),
        reverse=True,
    )
    return ranked


def _spot_count(band: dict[str, Any]) -> int:
    """Coerce a band dict's ``nearby_spot_count`` to int; garbage → 0."""

    value = band.get("nearby_spot_count")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def decide_switch(
    ranked: list[dict[str, Any]],
    *,
    idle: bool,
    current_freq_hz: Optional[int],
    seconds_since_last_switch: Optional[float],
    cooldown_s: float,
    seconds_since_manual_tune: Optional[float] = None,
    manual_respect_s: float = 3600.0,
) -> Optional[int]:
    """Pick a target dial frequency to tune to, or ``None`` to stay put.

    Pure function. Guards (any → no switch): orchestrator not idle; no
    ranked candidates; top band has no dial frequency; a switch happened
    within ``cooldown_s``; the top band is already the current one; the
    operator manually tuned the rig within ``manual_respect_s`` (2026-08-17:
    band_hunt used to fight the operator for 30 m — a manual band change
    must not be overridden by the hunter for a while).
    """
    if not idle or not ranked:
        return None
    target_freq = ranked[0].get("dial_freq_hz")
    if not isinstance(target_freq, int):
        return None
    if (
        seconds_since_last_switch is not None
        and seconds_since_last_switch < cooldown_s
    ):
        return None
    if (
        seconds_since_manual_tune is not None
        and seconds_since_manual_tune < manual_respect_s
    ):
        return None
    if current_freq_hz is not None and abs(current_freq_hz - target_freq) < MATCH_HZ:
        return None
    return target_freq


def filter_exhausted(
    ranked: list[dict[str, Any]],
    band_strikes: dict[str, int],
    max_strikes: int,
) -> list[dict[str, Any]]:
    """Drop bands whose hunt budget is exhausted; keep the rest in order.

    ``band_strikes[band]`` counts how many times the hunter pulled the rig
    back onto that band without a new DXCC completing.  A band over
    ``max_strikes`` is removed so the rig stops being dragged back to a
    band that does not yield (2026-08-17 field: 30 m/Congo re-pulled 7×).
    When the band's entities are worked (rank_bands drops it) or the band
    leaves the pskreporter report, the caller resets its strikes.
    """
    return [
        band
        for band in ranked
        if band_strikes.get(str(band.get("band", "")), 0) < max_strikes
    ]


def _avg_snr(band: dict[str, Any]) -> float:
    snr = band.get("avg_snr")
    try:
        return float(snr) if snr is not None else -30.0
    except (TypeError, ValueError):
        return -30.0
