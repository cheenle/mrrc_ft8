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
    if isinstance(payload, dict) and payload.get("ok") is True:
        return payload
    return None


def rank_bands(
    opportunities: dict[str, Any], worked_entities: set[str]
) -> list[dict[str, Any]]:
    """Keep bands with at least one unworked DXCC entity, ranked.

    Pure function. ``opportunities`` is the ``/api/band_hunt`` payload
    (``{"bands": [...]}``). ``worked_entities`` is the local worked set
    of DXCC entity names (e.g. ``{e.name for e in state.dxcc_cache.entities}``).

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
            e["name"] for e in entities if isinstance(e, dict) and e.get("name") not in worked_entities
        ]
        if not new_entities:
            continue
        ranked.append({**band, "new_entities": new_entities})
    ranked.sort(
        key=lambda b: (
            len(b["new_entities"]),
            int(b.get("nearby_spot_count", 0)),
            _avg_snr(b),
        ),
        reverse=True,
    )
    return ranked


def decide_switch(
    ranked: list[dict[str, Any]],
    *,
    idle: bool,
    current_freq_hz: Optional[int],
    seconds_since_last_switch: Optional[float],
    cooldown_s: float,
) -> Optional[int]:
    """Pick a target dial frequency to tune to, or ``None`` to stay put.

    Pure function. Guards (any → no switch): orchestrator not idle; no
    ranked candidates; top band has no dial frequency; a switch happened
    within ``cooldown_s``; the top band is already the current one.
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
    if current_freq_hz is not None and abs(current_freq_hz - target_freq) < MATCH_HZ:
        return None
    return target_freq


def _avg_snr(band: dict[str, Any]) -> float:
    snr = band.get("avg_snr")
    try:
        return float(snr) if snr is not None else -30.0
    except (TypeError, ValueError):
        return -30.0
