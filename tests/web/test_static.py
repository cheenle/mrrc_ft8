"""Static PWA contract regressions (§11.3, AD-011).

The cockpit has no browser in CI, so these tests pin the load-bearing
contracts: no inline application logic in ``index.html``, a landscape PWA
manifest, a service worker that never caches API/WS traffic, and every
referenced local asset present on disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "server" / "web" / "static"


def test_index_has_no_inline_javascript() -> None:
    html = (STATIC / "index.html").read_text()
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.DOTALL)
    assert scripts, "index.html must load its module entry point"
    for attributes, body in scripts:
        assert "src=" in attributes, "only external module scripts are allowed"
        assert 'type="module"' in attributes
        assert body.strip() == "", "no inline JS logic in index.html"
    assert 'type="module" src="/static/js/main.js"' in html


def test_referenced_assets_exist() -> None:
    html = (STATIC / "index.html").read_text()
    for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assert (STATIC.parent / ref.removeprefix("/static/")).exists() or (
            STATIC / ref.removeprefix("/static/")
        ).exists(), f"missing asset {ref}"


def test_manifest_is_landscape_pwa() -> None:
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    assert manifest["orientation"] == "landscape"
    assert manifest["display"] == "standalone"
    assert manifest["icons"], "PWA install needs an icon"
    for icon in manifest["icons"]:
        assert (STATIC / icon["src"].removeprefix("/static/")).exists()


def test_service_worker_never_caches_api_or_ws() -> None:
    sw = (STATIC / "sw.js").read_text()
    assert '/api/' in sw and '/ws/' in sw
    shell = re.search(r"SHELL = \[(.*?)\];", sw, re.DOTALL)
    assert shell is not None
    entries = re.findall(r'"(/static/[^"]+)"', shell.group(1))
    assert entries, "app shell must be pre-cached for offline start"
    for entry in entries:
        assert (STATIC / entry.removeprefix("/static/")).exists(), f"sw caches missing {entry}"
    assert "/api/" not in shell.group(1)


def test_css_keeps_landscape_and_portrait_degraded() -> None:
    css = (STATIC / "css" / "app.css").read_text()
    assert "@media (orientation: portrait)" in css
    assert "#workspace" in css and "#state-bar" in css


def test_form_fields_remain_typable_on_mobile() -> None:
    """Mobile browsers make inputs untypable under body user-select:none;
    the no-select rule must live on the cockpit, never on body."""

    css = (STATIC / "css" / "app.css").read_text()
    body_rule = css.split("body {", 1)[1].split("}", 1)[0]
    assert "user-select" not in body_rule
    cockpit_rule = css.split("#cockpit {", 1)[1].split("}", 1)[0]
    assert "user-select: none" in cockpit_rule


def test_hidden_attribute_keeps_display_none() -> None:
    """Author display rules (.overlay, #cockpit) override the UA
    `[hidden] { display: none }` rule; without an explicit guard the login
    overlay never hides and the absolute waterfall canvas paints over the
    login form, swallowing its taps (password field untypable)."""

    css = (STATIC / "css" / "app.css").read_text()
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", css)


def test_waterfall_resize_compares_floored_dimensions() -> None:
    """getBoundingClientRect returns fractional CSS pixels; comparing raw
    against the integer canvas.width/height mismatches every frame, and
    reassigning canvas size clears the bitmap — the waterfall never
    accumulates history. Resize must compare floored dimensions."""

    js = (STATIC / "js" / "waterfall.js").read_text()
    assert "canvas.width !== rect.width" not in js
    assert "canvas.height !== rect.height" not in js


def test_decode_dedup_scopes_to_callsign() -> None:
    """Band Activity keeps one row per station, newest decode wins (WSJT-X
    style). Keying by raw text alone would freeze stations whose CQ text
    repeats verbatim every slot, and rows without fresh timestamps would
    stop scrolling; replayed history batches must refresh rows too."""

    js = (STATIC / "js" / "state.js").read_text()
    assert "m.call || m.text" in js  # per-station identity, not raw text
    assert "slot_id" in js           # rows keep their slot for the UTC column
    assert "_t" in js                # newest-wins ordering key


def test_service_worker_cache_is_versioned() -> None:
    """Shell fixes must bump the cache name or clients keep stale assets."""

    sw = (STATIC / "sw.js").read_text()
    assert 'CACHE = "mrrc-ft8-shell-v' in sw


def test_candidates_render_band_activity_columns() -> None:
    js = (STATIC / "js" / "candidates.js").read_text()
    for field in ("snr", "dt", "freq", "text", "slot_id"):
        assert field in js
    assert "dblclick" in js  # double-click replies (same api.reply path)


def test_candidate_tap_never_fails_silently() -> None:
    """An observer tap on a decode row hits require_lease → 409; swallowed
    rejections leave the cockpit looking dead (console-only errors). The tap
    must take a free lease implicitly (WSJT-X-style single tap, §10.3/UC-002)
    and surface every rejection through the toast."""
    js = (STATIC / "js" / "candidates.js").read_text()
    assert "showToast" in js
    assert 'result.reason === "lease_required"' in js
    assert "api.acquireLease" in js


def test_toast_feedback_is_wired_and_cached() -> None:
    html = (STATIC / "index.html").read_text()
    assert 'id="toast"' in html
    css = (STATIC / "css" / "app.css").read_text()
    assert "#toast" in css
    sw = (STATIC / "sw.js").read_text()
    assert "/static/js/toast.js" in sw, "toast module must ship in the app shell"


def test_login_form_pairs_a_username_field() -> None:
    """Password managers and Chrome a11y audits expect a (visually hidden)
    username field alongside the station password."""
    html = (STATIC / "index.html").read_text()
    assert 'autocomplete="username"' in html
    css = (STATIC / "css" / "app.css").read_text()
    assert ".visually-hidden" in css


def test_auth_death_returns_to_login() -> None:
    """Sessions are in-memory (NFR-075), so a server restart wipes every
    cookie. Without an auth-death path the cockpit freezes on stale data
    with dead streams and no way back. REST 401s (outside /session/) and a
    4401 WS close must both route back to the login view."""
    api_js = (STATIC / "js" / "api.js").read_text()
    assert "status === 401" in api_js
    assert "location.reload()" in api_js
    streams_js = (STATIC / "js" / "streams.js").read_text()
    assert "4401" in streams_js
    assert "location.reload()" in streams_js


def test_api_cq_carries_loop_flag() -> None:
    js = (STATIC / "js" / "api.js").read_text()
    assert re.search(r"cq:\s*\(loop", js), "api.cq must accept a loop flag"


def test_api_exposes_clear_fault() -> None:
    js = (STATIC / "js" / "api.js").read_text()
    assert "clearFault" in js
    assert "/operation/clear-fault" in js


def test_safety_bar_offers_clear_fault_button() -> None:
    """A latched interlock fault locks TX until restart without a recovery
    path; the state bar must surface a CLEAR FAULT action wired to
    api.clearFault (§15.5 recovery, NFR-063)."""
    html = (STATIC / "index.html").read_text()
    assert 'id="btn-clear-fault"' in html
    assert "CLEAR FAULT" in html
    js = (STATIC / "js" / "safety.js").read_text()
    assert "btn-clear-fault" in js
    assert "api.clearFault" in js


def test_safety_bar_shows_cq_loop_countdown() -> None:
    js = (STATIC / "js" / "safety.js").read_text()
    assert "cq_loop" in js and "CQ LOOP" in js


def test_cq_loop_countdown_ticks_locally_between_snapshots() -> None:
    """The watchdog ticks every second without broadcasting, so a snapshot
    alone freezes the CQ LOOP countdown. safety.js must decrement locally
    on an interval and re-sync to idle_remaining_s on each new snapshot."""
    js = (STATIC / "js" / "safety.js").read_text()
    assert "setInterval" in js, "countdown needs a local per-second tick"
    assert "idle_remaining_s" in js, "tick re-syncs to the snapshot value"
    assert re.search(r"-=\s*1", js), "the tick decrements the remaining seconds"


def test_all_js_modules_parse_as_esm() -> None:
    for module in (STATIC / "js").glob("*.js"):
        text = module.read_text()
        assert "export " in text or module.name == "main.js"
        assert "require(" not in text  # no CommonJS in the build-step-free PWA


def test_stale_display_is_visibly_marked() -> None:
    """A dead stream must not look live: the waterfall canvas dims while its
    WS is offline (a frozen canvas impersonates a live band — 2026-08-02
    field confusion), candidate rows age into a stale class, and row time is
    the slot's own time so replayed history cannot re-float to the top."""

    wf = (STATIC / "js" / "waterfall.js").read_text()
    assert 'classList.toggle("offline"' in wf and "connected" in wf
    css = (STATIC / "css" / "app.css").read_text()
    assert "#waterfall-canvas.offline" in css
    assert ".candidate.stale" in css
    candidates = (STATIC / "js" / "candidates.js").read_text()
    assert "STALE_AFTER_MS" in candidates
    state = (STATIC / "js" / "state.js").read_text()
    assert "slot_id * 15_000" in state
