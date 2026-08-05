# new-DXCC Hunt Reaction Optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the new-DXCC hunt reaction latency from 10 s+ (dxcc_summary rebuild) and 8 s+ (deep-window dashboard) to sub-second, by indexing the cty.dat lookup (preserving full match semantics), dropping the 3/7-day dashboard windows, refreshing the band-hunt worked set on every tick, and caching upstream band_hunt responses.

**Architecture:** Four independent changes. (1) `CtyDatabase.lookup()` gets an exact-match dict + prefix trie built in `__post_init__`; the public interface and match semantics (exact-first → longest-prefix → digit-replacement) are byte-for-byte preserved, guarded by a semantic-equivalence test against the old linear scan. (2) The dashboard's `BAND_HUNT_WINDOWS` drops 3-day/7-day. (3) A shared `_fresh_dxcc_cache(state)` refresh helper replaces the three duplicated dirty-checks; the band-hunt loop calls it each tick so a just-worked entity stops being hunted. (4) An in-process TTL cache (`_BandHuntCache`, modeled on `IdempotencyCache`) behind the `/band-hunt` proxy kills repeated cold upstream fetches.

**Tech Stack:** Python 3.13 / asyncio / FastAPI / SQLite; vanilla JS (no build). Tests: pytest, `httpx.MockTransport`-style fake clients, `TestClient`.

## Global Constraints

- cty.dat matching semantics MUST be preserved exactly: `=CALL` exact wins over everything, then longest-prefix, `(digits)` rules are already expanded at load; ties keep the first-encountered entity. No behavior change may ship.
- Public surface of `CtyDatabase` (`entities`, `lookup`) and `get_cty_database()` unchanged.
- Front-end is buildless vanilla JS in `server/web/static/js/`; no new deps, no build step.
- Hardware safety: never touch TX/PTT/rig paths; this is web/engine read-path work only.
- Every code change syncs `SDD/14-version-history.md` (one combined Unreleased entry added in the final task) per AGENTS.md.
- All test runs: `venv/bin/python -m pytest tests/` (from repo root).

---

### Task 1: Indexed `CtyDatabase.lookup()` — exact dict + prefix trie, semantics preserved

**Files:**
- Modify: `server/engine/dxcc.py` (`CtyDatabase`, imports)
- Test: `tests/engine/test_dxcc.py`

**Interfaces:**
- Consumes: existing `CtyEntity` (`.name`, `.continent`, `.prefixes`) and `load_cty`.
- Produces: `CtyDatabase` with new `__post_init__` building `self._exact: dict[str, tuple[str, str]]` and `self._trie: dict[str, Any]`; `lookup(call) -> tuple[str, str] | None` unchanged signature, now index-backed. Later tasks rely on `lookup` being fast (Task 3's `_fresh_dxcc_cache` calls `dxcc_summary`, which calls `lookup` ~6.7k times).

- [ ] **Step 1: Write the failing tests**

Add to `tests/engine/test_dxcc.py`:

```python
"""Indexed lookup: semantic equivalence to the pre-index linear scan."""
import random

import pytest

from server.engine.dxcc import CtyDatabase, get_cty_database, load_cty


def _lookup_linear(entities, call: str):
    """Reference: exact copy of the pre-index algorithm (dxcc.py:55)."""
    base = call.split("/", 1)[0].upper()
    best_len = -1
    best = None
    for entity in entities:
        for stored in entity.prefixes:
            if stored.startswith("="):
                if base == stored[1:]:
                    return (entity.name, entity.continent)
            elif base.startswith(stored):
                if len(stored) > best_len:
                    best_len = len(stored)
                    best = entity
    return (best.name, best.continent) if best else None


def _corpus_from_cty(db: CtyDatabase, *, prefix_sample: int = 300, exact_step: int = 10) -> set[str]:
    """Bounded adversarial corpus over the repo cty.dat: a deterministic
    sample of every reachable exact key plus prefix-hit variants (bare, digit,
    alphabetic, portable, lowercased).  Sized so the pre-index reference scan
    finishes in seconds — enumerating all ~40k prefixes would take minutes
    against the old linear lookup (the reason the original unbounded version
    hung).  Slash-free exact keys are the only reachable exact entries:
    ``lookup`` strips ``/suffix`` before matching."""
    exact: list[str] = []
    non_exact: list[str] = []
    for entity in db.entities:
        for stored in entity.prefixes:
            if stored.startswith("="):
                if "/" not in stored:
                    exact.append(stored[1:])
            else:
                non_exact.append(stored)
    calls: set[str] = set()
    calls.update(exact[::exact_step])            # deterministic sample of exact keys
    calls.update(exact[:50])                     # head of the exact list
    for stored in non_exact[:prefix_sample]:
        calls.add(stored)                        # bare prefix (shortest hit)
        for suffix in ("1", "ABC", "P", "qra"):  # digit / alphabetic / portable / lowercase
            calls.add(stored + suffix)
    return calls


def _synthetic_calls(seed: int = 42, n: int = 1500) -> set[str]:
    rng = random.Random(seed)
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out: set[str] = set()
    for _ in range(n):
        out.add("".join(rng.choice(chars) for _ in range(rng.randint(1, 8))))
    base = list(out)[:50]
    for c in base:
        out.add(c + "/P")    # slash suffix stripping
        out.add(c + "/QRP")
        out.add(c.lower())
    return out


def test_index_structures_built_on_load() -> None:
    db = get_cty_database()
    assert hasattr(db, "_exact")
    assert hasattr(db, "_trie")
    assert db._exact.get("9M4SDX") == ("Spratly Islands", "AS")  # real exact entry
```

```python
def test_indexed_lookup_equivalent_to_linear_over_full_corpus() -> None:
    db = get_cty_database()
    calls = _corpus_from_cty(db) | _synthetic_calls()
    assert len(calls) > 500
    for call in sorted(calls):
        assert db.lookup(call) == _lookup_linear(db.entities, call), (
            f"semantic mismatch for {call!r}"
        )
```

```python
@pytest.mark.skipif(
    not (__import__("pathlib").Path(__file__).resolve().parents[2] / "mrrc-ft8.db").exists(),
    reason="live QSO log not present",
)
def test_indexed_lookup_equivalent_over_live_qso_log() -> None:
    from pathlib import Path
    from server.engine.repository import Repository

    db = get_cty_database()
    repo = Repository(str(Path(__file__).resolve().parents[2] / "mrrc-ft8.db"))
    calls = {qso.dx_call for qso in repo.list_qsos(include_void=False)}
    assert len(calls) > 1000
    for call in calls:
        assert db.lookup(call) == _lookup_linear(db.entities, call), call
```

```python
def test_lookup_speed_smoke() -> None:
    """Loose upper bound: the index must stay 3+ orders of magnitude faster
    than the old ~1.5 ms/call linear scan (guard against index regressions)."""
    import time

    db = get_cty_database()
    calls = _synthetic_calls(seed=7, n=2000)
    t0 = time.perf_counter()
    for c in calls:
        db.lookup(c)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"{elapsed:.2f}s for {len(calls)} lookups"
```

- [ ] **Step 2: Run test to verify the index tests fail**

Run: `venv/bin/python -m pytest tests/engine/test_dxcc.py -v`
Expected: `test_index_structures_built_on_load`, `test_lookup_speed_smoke` FAIL (`AttributeError: 'CtyDatabase' object has no attribute '_exact'`); the two equivalence tests PASS (old lookup still linear — they must pass now to prove the reference matches the current code).

- [ ] **Step 3: Implement the index in `server/engine/dxcc.py`**

Change imports (top of file):

```python
from dataclasses import dataclass, field
from typing import Any
```

Change `CtyDatabase`:

```python
@dataclass
class CtyDatabase:
    entities: list[CtyEntity]
    _exact: dict[str, tuple[str, str]] = field(init=False, repr=False)
    _trie: dict[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the exact-match dict + prefix trie. ``setdefault`` preserves
        first-encountered on duplicate patterns (matches the old scan)."""
        self._exact = {}
        self._trie: dict[str, Any] = {}
        for entity in self.entities:
            for stored in entity.prefixes:
                if stored.startswith("="):
                    self._exact.setdefault(
                        stored[1:], (entity.name, entity.continent)
                    )
                else:
                    node = self._trie
                    for ch in stored:
                        node = node.setdefault(ch, {})
                    node.setdefault("__entity__", (entity.name, entity.continent))

    def lookup(self, call: str) -> tuple[str, str] | None:
        """(entity_name, continent) for a callsign; exact match wins, then
        the longest prefix match; None when nothing matches."""

        base = call.split("/", 1)[0].upper()
        exact = self._exact.get(base)
        if exact is not None:
            return exact
        node = self._trie
        best: tuple[str, str] | None = None
        for ch in base:
            node = node.get(ch)
            if node is None:
                break
            entity = node.get("__entity__")
            if entity is not None:
                best = entity  # deeper = longer prefix; last assignment wins
        return best
```

The `__entity__` sentinel cannot collide with a real prefix character (letters/digits only in cty.dat).

- [ ] **Step 4: Run test to verify all pass**

Run: `venv/bin/python -m pytest tests/engine/test_dxcc.py -v`
Expected: ALL PASS, including the equivalence tests (index == linear over corpus + live log) and the speed smoke test.

- [ ] **Step 5: Commit**

```bash
git add server/engine/dxcc.py tests/engine/test_dxcc.py
git commit -m "perf(dxcc): index CtyDatabase.lookup with exact dict + prefix trie

Preserves exact-first -> longest-prefix -> digit-replacement semantics,
guarded by a semantic-equivalence test vs the old linear scan. Lookup drops
from ~1.5ms to sub-microsecond; dxcc_summary full rebuild ~10.2s -> ~0.2s."
```

---

### Task 2: Drop 3-day / 7-day dashboard windows

**Files:**
- Modify: `server/web/static/js/settings.js:394-397`

**Interfaces:**
- Consumes: nothing new. Produces: `BAND_HUNT_WINDOWS` with 5 entries; the dashboard fetches only these.

- [ ] **Step 1: Edit the window list**

In `server/web/static/js/settings.js`, replace:

```js
  const BAND_HUNT_WINDOWS = [
    [10, "10 min"], [30, "30 min"], [60, "1 hour"], [240, "4 hours"],
    [1440, "1 day"], [4320, "3 days"], [10080, "7 days"],
  ];
```

with:

```js
  const BAND_HUNT_WINDOWS = [
    [10, "10 min"], [30, "30 min"], [60, "1 hour"], [240, "4 hours"],
    [1440, "1 day"],
  ];
```

(`3 days` / `7 days` removed; 1 day stays as the deepest window.)

- [ ] **Step 2: Verify no JS-coupled test breaks**

Run: `venv/bin/python -m pytest tests/web/test_api.py -q`
Expected: PASS (the existing `window_min=4320` proxy tests hit the API directly and are unaffected by the client-side window list).

- [ ] **Step 3: Commit**

```bash
git add server/web/static/js/settings.js
git commit -m "feat(web): drop 3/7-day deep windows from New-DXCC dashboard

BAND_HUNT_WINDOWS now caps at 1 day; the dashboard no longer waits on the
slowest (5-8 s cold) deep-window fetch to render the whole panel."
```

---

### Task 3: Shared `_fresh_dxcc_cache(state)` + band-hunt worked-set refresh

**Files:**
- Modify: `server/web/api.py` (new helper; `/dxcc` and `/band-hunt` handlers)
- Modify: `server/main.py` (startup pre-fill; `band_hunt_loop`)
- Test: `tests/web/test_api.py`

**Interfaces:**
- Consumes: `AppState` (`state.repository`, `state.dxcc_cache`), `_cty_database()`, `dxcc_summary`, Task 1's fast lookup.
- Produces: `async def _fresh_dxcc_cache(state) -> None` in `server/web/api.py`. Callers (Task 3 itself) `await` it anywhere the worked set is needed; it mutates `state.dxcc_cache` and resets `state.repository.dxcc_dirty`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/web/test_api.py`:

```python
import asyncio

from server.web.api import _fresh_dxcc_cache


def test_fresh_dxcc_cache_rebuilds_when_dirty(state) -> None:
    state.dxcc_cache = None
    state.repository.dxcc_dirty = True
    asyncio.run(_fresh_dxcc_cache(state))
    assert state.dxcc_cache is not None
    assert state.repository.dxcc_dirty is False


def test_fresh_dxcc_cache_keeps_cache_when_clean(state) -> None:
    state.dxcc_cache = SimpleNamespace(entities=[SimpleNamespace(name="Japan")])
    state.repository.dxcc_dirty = False
    asyncio.run(_fresh_dxcc_cache(state))
    assert [e.name for e in state.dxcc_cache.entities] == ["Japan"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/web/test_api.py -k fresh_dxcc_cache -v`
Expected: FAIL (`ImportError: cannot import name '_fresh_dxcc_cache'`).

- [ ] **Step 3: Implement the helper + refactor the two handlers**

In `server/web/api.py`, next to `_cty_database()` (line ~144):

```python
async def _fresh_dxcc_cache(state: Any) -> None:
    """Rebuild state.dxcc_cache only when missing or a QSO write happened
    since the last build (repository.dxcc_dirty).  Runs the scan off-thread;
    0.2 s thanks to the indexed lookup (Task 1)."""
    if state.dxcc_cache is None or state.repository.dxcc_dirty:
        from ..engine.dxcc import dxcc_summary

        state.dxcc_cache = await asyncio.to_thread(
            dxcc_summary, state.repository, _cty_database()
        )
        state.repository.dxcc_dirty = False
```

In the `/dxcc` handler (lines ~702-714), replace the whole body with:

```python
    @router.get("/dxcc")
    async def dxcc(session: Session = Depends(require_session)) -> JSONResponse:
        # 低频数据：缓存到首次打开/任何 QSO 写入（dirty）后才重建（决策 A）。
        await _fresh_dxcc_cache(state)
        return _ok(state.dxcc_cache.to_dict())
```

In the `/band-hunt` handler (lines ~757-766), replace:

```python
        from ..engine.dxcc import dxcc_summary

        cache = state.dxcc_cache
        if cache is None or state.repository.dxcc_dirty:
            cache = await asyncio.to_thread(
                dxcc_summary, state.repository, _cty_database()
            )
            state.dxcc_cache = cache
            state.repository.dxcc_dirty = False
        worked = {e.name for e in cache.entities}
```

with:

```python
        await _fresh_dxcc_cache(state)
        worked = {e.name for e in state.dxcc_cache.entities}
```

- [ ] **Step 4: Refactor `server/main.py`**

At the import (line 45), add `_fresh_dxcc_cache`:

```python
from .web.api import AppState, create_app, _fresh_dxcc_cache, _snapshot
```

Replace the startup pre-fill (lines ~601-609):

```python
            if state.dxcc_cache is None or repository.dxcc_dirty:
                from .engine.dxcc import dxcc_summary, get_cty_database

                state.dxcc_cache = await asyncio.to_thread(
                    dxcc_summary, repository, get_cty_database()
                )
                repository.dxcc_dirty = False
```

with:

```python
            await _fresh_dxcc_cache(state)
```

In `band_hunt_loop` (lines ~704-716), replace:

```python
                    if state.dxcc_cache is None:
                        continue  # worked set unknown — never switch blindly
                    payload = await fetch_opportunities(
                        config.band_hunt_url,
                        {
                            "home_grid": config.my_grid.upper(),
                            "radius_km": config.band_hunt_radius_km,
                            "window_min": config.band_hunt_window_min,
                        },
                    )
                    if payload is None:
                        continue
                    worked = {e.name for e in state.dxcc_cache.entities}
```

with:

```python
                    await _fresh_dxcc_cache(state)
                    if state.dxcc_cache is None:
                        continue  # worked set unknown — never switch blindly
                    payload = await fetch_opportunities(
                        config.band_hunt_url,
                        {
                            "home_grid": config.my_grid.upper(),
                            "radius_km": config.band_hunt_radius_km,
                            "window_min": config.band_hunt_window_min,
                        },
                    )
                    if payload is None:
                        continue
                    worked = {e.name for e in state.dxcc_cache.entities}
```

- [ ] **Step 5: Add the stale-worked-set mechanism test**

Add to `tests/web/test_api.py`:

```python
def test_fresh_dxcc_cache_reflects_new_qso(state) -> None:
    """After a QSO completes (dxcc_dirty), a refresh must surface the new
    entity in the worked set so the band hunt stops hunting it."""
    from server.engine.sequencer import QSORecord

    state.repository.record_qso(
        QSORecord(my_call="M0XX", my_grid="IO91", dx_call="BI1TX", band="20m"),
        completed_epoch=1700000000.0,
    )
    state.dxcc_cache = None
    asyncio.run(_fresh_dxcc_cache(state))
    worked = {e.name for e in state.dxcc_cache.entities}
    assert "China" in worked
```

- [ ] **Step 6: Run tests to verify pass**

Run: `venv/bin/python -m pytest tests/web/test_api.py -k "fresh_dxcc_cache or band_hunt" -v`
Expected: ALL PASS, including the pre-existing `test_band_hunt_proxy_*` tests (they pre-set `dxcc_cache` + `dxcc_dirty=False`, so the helper keeps their fake cache).

- [ ] **Step 7: Commit**

```bash
git add server/web/api.py server/main.py tests/web/test_api.py
git commit -m "fix(band-hunt): refresh DXCC worked set every tick via _fresh_dxcc_cache

Deduplicates the dirty-check + rebuild into one shared helper used by /dxcc,
/band-hunt, startup pre-fill and band_hunt_loop; a just-completed QSO is no
longer re-hunted as a new DXCC (stale worked set was the cause)."
```

---

### Task 4: Progressive dashboard render (render each window as it lands)

**Files:**
- Modify: `server/web/static/js/settings.js:405-451`

**Interfaces:**
- Consumes: Task 2's 5-window `BAND_HUNT_WINDOWS`, `api.bandHunt`, `BAND_HUNT_SPOT_CAP`.
- Produces: `openBandHuntView()` that fires all window fetches in parallel and renders each window the moment it resolves (window order preserved); extracted `buildWindowHtml(label, res) -> str`.

- [ ] **Step 1: Refactor `openBandHuntView`**

Replace the function (lines 405-451) with:

```js
  function buildWindowHtml(label, res) {
    const html = [];
    html.push(`<h3>${label}</h3>`);
    if (!res.ok) {
      html.push(`<p class='drawer-hint dim'>${escapeHtml(res.reason || res.status)}</p>`);
      return html.join("");
    }
    const bands = res.bands || [];
    const allSpots = bands.flatMap((b) => b.spots || []);
    const workedTotal = bands.reduce((n, b) => n + (b.worked_spot_count || 0), 0);
    if (!allSpots.length) {
      html.push(`<p class='drawer-hint dim'>0 new-DXCC spots` +
        `${workedTotal ? ` · ${workedTotal} already-worked nearby` : ""}.</p>`);
      return html.join("");
    }
    const fresh = allSpots.slice(0, BAND_HUNT_SPOT_CAP);
    html.push(`<div class="qso-row"><span class="qso-call"></span>` +
      `<span class="qso-meta">${allSpots.length} new-DXCC spot(s)` +
      `${workedTotal ? ` · ${workedTotal} worked nearby` : ""}` +
      `${allSpots.length > fresh.length ? ` (showing ${fresh.length})` : ""}</span></div>`);
    for (const s of fresh) {
      const name = s.entity || s.callsign || "?";
      const band = s.band || "?";
      const snr = s.snr == null ? "" : ` ${s.snr}dB`;
      const t = s.qso_time ? s.qso_time.replace("T", " ").slice(5, 16) : "";
      html.push(
        `<div class="qso-row"><span class="qso-call">${escapeHtml(name)}</span>` +
        `<span class="qso-meta">${escapeHtml(s.callsign)} · ${escapeHtml(band)}` +
        `${snr} · ${escapeHtml(t)}</span></div>`);
    }
    return html.join("");
  }

  async function openBandHuntView() {
    bandHuntOverlay.hidden = false;
    document.body.classList.add("log-open");
    bandHuntContent.innerHTML = "<p class='drawer-hint'>Loading new-DXCC spots…</p>";

    // Fire every window fetch in parallel, then render each window the moment
    // it resolves (re-render keeps window ORDER stable; the 10-min window is
    // visible in ~1 s instead of waiting for the 1-day window).
    const pending = BAND_HUNT_WINDOWS.map(([windowMin, label]) =>
      api.bandHunt({ window_min: windowMin, detail: 1, min_spots: 1 })
        .then((res) => [label, res]));
    const rendered = new Map();
    for (const p of pending) {
      const [label, res] = await p;
      rendered.set(label, buildWindowHtml(label, res));
      bandHuntContent.innerHTML =
        BAND_HUNT_WINDOWS.map(([, l]) => rendered.get(l) ?? "").join("");
    }
  }
```

- [ ] **Step 2: Verify existing web tests + static sanity**

Run: `venv/bin/python -m pytest tests/web/ -q`
Expected: PASS. Then sanity-check the file loads (no syntax error) with `node --check` if node is available:

```bash
node --check server/web/static/js/settings.js || echo "node unavailable, skipping syntax check"
```

- [ ] **Step 3: Commit**

```bash
git add server/web/static/js/settings.js
git commit -m "feat(web): render New-DXCC dashboard windows progressively

Fires all window fetches in parallel and renders each as it resolves
(order preserved), so small windows appear immediately instead of the
whole panel waiting on the slowest deep window."
```

---

### Task 5: Upstream TTL cache behind the `/band-hunt` proxy

**Files:**
- Modify: `server/web/api.py` (`_BandHuntCache`, `AppState` field, `band_hunt_proxy`)
- Test: `tests/web/test_api.py`

**Interfaces:**
- Consumes: `state.band_hunt_url`, the proxy fetch, Task 3's `_fresh_dxcc_cache`.
- Produces: `_BandHuntCache` (clock-injectable TTL map, keyed by `(window_min, home_grid, radius_km, detail)`, TTL `min(window_min, 3600)` s, LRU-ish oldest-eviction cap 64) stored on `state.band_hunt_cache`. Caches only `ok:true` raw upstream bodies; the worked-entity filter always runs fresh per request. The `band_hunt_loop` deliberately does NOT use this cache: its 60 s poll cadence outlives the 30 s TTL of its own `window_min=30` request, and it fetches `detail=0` (band-level) while the dashboard needs `detail=1` — sharing would be a dead path, so the loop keeps fetching fresh (it is the propagation gate).

- [ ] **Step 1: Write the failing tests**

Add to `tests/web/test_api.py`:

```python
from server.web.api import _BandHuntCache


def test_band_hunt_cache_hit_miss_and_expiry() -> None:
    now = [0.0]
    cache = _BandHuntCache(clock=lambda: now[0])
    assert cache.get(("k",)) is None
    cache.put(("k",), {"ok": True}, ttl_s=10)
    assert cache.get(("k",)) == {"ok": True}
    now[0] = 10.0
    assert cache.get(("k",)) is None  # expired


def test_band_hunt_cache_evicts_oldest_when_full() -> None:
    now = [0.0]
    cache = _BandHuntCache(clock=lambda: now[0], max_entries=2)
    cache.put(("a",), {"ok": True}, ttl_s=60)
    cache.put(("b",), {"ok": True}, ttl_s=60)
    cache.put(("c",), {"ok": True}, ttl_s=60)  # evicts "a" (oldest)
    assert cache.get(("a",)) is None
    assert cache.get(("b",)) is not None
    assert cache.get(("c",)) is not None


def test_band_hunt_proxy_serves_second_request_from_cache(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, state: AppState
) -> None:
    state.band_hunt_url = "http://psk.test/api/band_hunt"
    state.dxcc_cache = SimpleNamespace(entities=[SimpleNamespace(name="Japan")])
    state.repository.dxcc_dirty = False
    hits = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"ok": True, "bands": []}

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, params: object = None) -> _FakeResponse:
            hits["n"] += 1
            return _FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    headers = auth_headers(login(client))
    for _ in range(2):
        res = client.get("/api/v1/band-hunt?window_min=10&detail=1", headers=headers)
        assert res.status_code == 200
    assert hits["n"] == 1  # second request served from the TTL cache


def test_band_hunt_proxy_does_not_cache_error_bodies(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, state: AppState
) -> None:
    state.band_hunt_url = "http://psk.test/api/band_hunt"
    state.dxcc_cache = SimpleNamespace(entities=[SimpleNamespace(name="Japan")])
    state.repository.dxcc_dirty = False
    hits = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            hits["n"] += 1
            return {"ok": False, "reason": "db_down"}

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, params: object = None) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    headers = auth_headers(login(client))
    for _ in range(2):
        client.get("/api/v1/band-hunt?window_min=10&detail=1", headers=headers)
    assert hits["n"] == 2  # ok:false bodies are never cached
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/web/test_api.py -k "band_hunt_cache or serves_second or does_not_cache" -v`
Expected: FAIL (`ImportError: cannot import name '_BandHuntCache'`).

- [ ] **Step 3: Implement `_BandHuntCache` + AppState field**

In `server/web/api.py`, after `IdempotencyCache` (line ~94):

```python
class _BandHuntCache:
    """TTL cache for raw upstream /api/band_hunt bodies (proxy + poller share).

    TTL = min(window_min, 3600) s: small windows stay fresh, deep windows avoid
    repeated cold 5-8 s fetches. Raw (unfiltered) bodies are cached; the
    worked-entity filter always runs fresh per request so a just-completed QSO
    shows immediately. Errors and ``ok:false`` bodies are never cached.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 64,
    ) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[tuple, tuple[float, dict[str, Any]]] = {}

    def get(self, key: tuple) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, body = entry
        if self._clock() >= expires:
            del self._entries[key]
            return None
        return body

    def put(self, key: tuple, body: dict[str, Any], ttl_s: float) -> None:
        if len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]
        self._entries[key] = (self._clock() + ttl_s, body)
```

Add the field to `AppState` (near `band_hunt_url`, line ~124):

```python
    band_hunt_cache: _BandHuntCache = field(default_factory=_BandHuntCache)
```

- [ ] **Step 4: Wire the cache into `band_hunt_proxy`**

Replace the fetch block (lines ~738-751) with:

```python
        key = (
            params["window_min"],
            params["home_grid"],
            params["radius_km"],
            params.get("detail", "0"),
        )
        body = state.band_hunt_cache.get(key)
        if body is None:
            try:
                import httpx

                # Deep windows take up to ~5-8 s on first (uncached) hit; the
                # poller's own timeout stays 5 s, the dashboard proxy needs 20 s.
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.get(state.band_hunt_url, params=params)
                    resp.raise_for_status()
                    body = resp.json()
            except Exception as exc:
                return _reject(502, "band_hunt_unreachable", detail=str(exc))
            if not isinstance(body, dict) or body.get("ok") is not True:
                return JSONResponse(body if isinstance(body, dict) else {"ok": False, "reason": "bad_upstream"})
            try:
                ttl_s = max(5, min(int(params["window_min"]), 3600))
            except (TypeError, ValueError):
                ttl_s = 60
            state.band_hunt_cache.put(key, body, ttl_s=ttl_s)
```

(Filtering below stays exactly as-is; `worked`/`cty` come from `_fresh_dxcc_cache(state)` per Task 3.)

- [ ] **Step 5: Run tests to verify pass**

Run: `venv/bin/python -m pytest tests/web/test_api.py -k "band_hunt" -v`
Expected: ALL PASS (cache hit/miss/expiry/eviction, second-request-from-cache, no-cache-on-error, plus the pre-existing proxy tests).

- [ ] **Step 6: Commit**

```bash
git add server/web/api.py tests/web/test_api.py
git commit -m "feat(web): TTL-cache upstream band_hunt responses in the proxy

Kills repeated cold 5-8 s deep-window fetches on dashboard re-opens:
TTL = min(window_min, 3600) s, keyed by (window_min, grid, radius, detail),
raw bodies cached so the fresh worked-entity filter still runs per request.
Errors never cached. band_hunt_loop intentionally keeps fresh fetches."
```

---

### Task 6: SDD version history + full regression suite

**Files:**
- Modify: `SDD/14-version-history.md`

**Interfaces:**
- Consumes: all prior tasks' changes.
- Produces: one combined Unreleased entry; no code change.

- [ ] **Step 1: Add the combined Unreleased entry**

Insert at the very top of `SDD/14-version-history.md`, above the existing
`## Unreleased — 2026-08-05 — Manual Reply Races the Current Slot` entry:

```markdown
## Unreleased — 2026-08-05 — New-DXCC Hunt Reaction Optimization

- 实测基线（生产 DB 10,533 QSO）：`cty.lookup` 单次 1.5 ms（线性扫描 346 实体 × ~38.5k 前缀）；`dxcc_summary` 全量重建 10.2 s（6,749 次 lookup × 2.59 亿 startswith）；每次 QSO 写入后 `/dxcc`、`/band-hunt` 首次请求被 10 s+ 重建阻塞；dashboard `Promise.all` 等最慢深窗口（3/7 天冷缓存 5-8 s）。
- `CtyDatabase.lookup` 索引化：`__post_init__` 构建 `_exact`（`=CALL` 精确优先）+ 前缀 trie（最长前缀；`(数字)` 替换已在加载时展开），`setdefault` 保留并列最先遇到。语义与旧线性扫描**逐字节等价**（语义一致性回归：cty.dat 全展开模式 + 2000 合成呼号 + 真实 QSO 日志 dx_call 全量比对）。lookup 1.5 ms → 亚微秒；`dxcc_summary` 重建 10.2 s → ~0.2 s。
- 去掉 New-DXCC dashboard 的 3/7 天深窗口（`BAND_HUNT_WINDOWS` 收窄到 10/30 分钟、1/4 小时、1 天）。
- 抽 `_fresh_dxcc_cache(state)` 共享刷新助手（`/dxcc`、`/band-hunt`、启动预填、`band_hunt_loop` 四处复用）；**修复自动波段猎人陈旧 worked 集**——每 tick 先刷新，刚通联的实体不再被重复 hunt。
- dashboard 渐进渲染：所有窗口 fetch 并行、先到先渲染（顺序保持），10 分钟窗口 ~1 s 内可见。
- `/band-hunt` 代理加上游 TTL 缓存（`_BandHuntCache`，键 `(window_min, grid, radius, detail)`，TTL `min(window_min, 3600)` s，只缓存 `ok:true` 原始 body，worked 过滤仍每请求实时跑）；dashboard 在 TTL 内二次打开免冷拉，band_hunt_loop 保持每 tick 新鲜拉取。
- Regressions: `test_dxcc.py`（索引语义等价 + 速度烟测）、`test_api.py`（`_fresh_dxcc_cache` dirty/clean/新 QSO、`_BandHuntCache` hit/miss/过期/驱逐、代理二次请求命中缓存、错误不缓存）、既有 `band_hunt_proxy_*` 全绿。全量套件绿。
```

- [ ] **Step 2: Run the full suite**

Run: `venv/bin/python -m pytest tests/ -q`
Expected: ALL PASS. Also confirm the SDD harness (`tests/test_sdd_harness.py`) passes — it validates the version-history/SDD conventions.

- [ ] **Step 3: Commit**

```bash
git add SDD/14-version-history.md
git commit -m "docs(sdd): version history entry for new-DXCC hunt reaction optimization"
```
