# MRRC-FT8 QSO Log Page — Design

**Date:** 2026-08-04
**Status:** Draft for review
**Scope:** Add a "Log" menu entry in the cockpit's settings drawer that opens a full-screen overlay listing the QSO log. Approach A (overlay logic lives in `settings.js`; no new JS module). No backend changes.

## 1. Purpose

Today the QSO log is rendered inline inside the settings drawer's `Log` tab. The operator wants a dedicated view: the drawer's `Log` entry becomes a menu item that opens a **full-screen overlay** on top of the cockpit (the waterfall/candidates stay visible behind it) listing the QSO log in a larger, scrollable format with an ADIF export link.

Confirmed decisions (brainstorm, 2026-08-04):
- Entry point: the settings drawer's `Log` tab, acting as a menu item (replaces the inline tab).
- Page form: full-screen overlay on top of the cockpit.
- Content: simple list (DX call, mode/band, completed UTC) + ADIF export + close button.
- Implementation: Approach A — all logic in `settings.js`; no new JS module.

## 2. Current Structure

- `server/web/static/index.html` — SPA shell: `#cockpit` (top bar, `#workspace`, `#settings-drawer`, state bar). The drawer's nav has `data-tab="log"` (index.html:56).
- `server/web/static/js/settings.js` — `createSettingsDrawer()` wires tabs; `renderLog()` fetches `api.qsos()` and renders `.qso-row` items into the drawer `#drawer-content`.
- `server/web/static/js/api.js` — `qsos()` → `GET /api/v1/logs/qsos`; the ADIF export is a plain link to `/api/v1/logs/adif` (download attribute).
- `server/web/static/css/app.css` — CSS variables `--bg/--panel/--edge/--text/--dim/--accent/--danger/--ok`; existing `.qso-row/.qso-call/.qso-meta/.btn.adif/.drawer-hint` styles.

## 3. Design

### 3.1 index.html — overlay markup

Add a hidden full-screen overlay after `#settings-drawer`:

```html
<!-- Full-screen QSO log -->
<div id="log-overlay" class="log-overlay" hidden>
  <div class="log-panel">
    <header class="log-header">
      <strong>QSO Log</strong>
      <a class="btn adif" href="/api/v1/logs/adif" download>Export ADIF</a>
      <button id="btn-log-close" class="icon-btn" aria-label="Close log">✕</button>
    </header>
    <div id="log-list" class="log-list"></div>
  </div>
</div>
```

### 3.2 settings.js — overlay open/close + rendering

- Replace the `Log` tab's inline behaviour: in the tab/switch handler, `data-tab === "log"` now calls `close()` (close the drawer) then `openLogView()` instead of `renderLog()`.
- Add `openLogView()`:
  1. `#log-overlay.hidden = false`; `document.body.classList.add("log-open")`.
  2. Set `#log-list` to a "Loading QSO log…" hint, then `await api.qsos()`.
  3. On success render the same `.qso-row` list the drawer used, but into `#log-list`; on failure show `Could not load log: <reason>`.
  4. Update the header count (`<span class="count">`).
- Add `closeLogView()`: hide `#log-overlay`, remove `log-open` body class.
- Wire `#btn-log-close` and the `#log-overlay` backdrop (click on backdrop outside the panel closes) to `closeLogView()`.
- Remove the inline `renderLog` body (its row-rendering moves into `openLogView`; no other caller).

### 3.3 css/app.css — overlay styles

```css
.log-overlay { position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,0.65);
  display: flex; align-items: center; justify-content: center; padding: 1.5rem; }
.log-panel { width: min(880px, 100%); max-height: 88vh; background: var(--panel);
  border: 1px solid var(--edge); border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; }
.log-header { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  border-bottom: 1px solid var(--edge); }
.log-header strong { flex: 1; }
.log-list { overflow-y: auto; padding: 6px 16px 16px; }
.log-list .qso-row { font-size: 14px; padding: 8px 2px; }
```

Backdrop click handling: clicking `#log-overlay` itself (not the panel) closes it — the panel stops propagation.

## 4. Data Flow

Drawer `Log` click → close drawer → `openLogView()` → `#log-overlay` visible → `api.qsos()` (`GET /api/v1/logs/qsos`, session cookie) → render `.qso-row` items (call / mode+band / completed UTC) into `#log-list` + count → ADIF export link (`/api/v1/logs/adif`, download). Close button or backdrop click hides the overlay; the cockpit state is untouched (no reload).

## 5. Error Handling

- `api.qsos()` failure: show `Could not load log: <reason or status>` inside the overlay; the overlay remains open so the operator can close it.
- Empty log: render the existing "No QSOs yet." hint.
- Overlay is DOM-only state; closing it never touches the rig, sequencer or lease.

## 6. Testing

- Backend `GET /api/v1/logs/qsos` and `/logs/adif` are already covered by `tests/web/test_api.py` (no backend change).
- Frontend is vanilla JS without a JS test framework: verified manually on the running cockpit (login → drawer → Log → overlay lists QSOs; ADIF downloads; close returns to a live cockpit; empty and error states shown).

## 7. Non-Goals

- No separate HTML page, routing, or pagination.
- No search/filter (deferred unless requested).
- No changes to the `qsos` API shape or the drawer's other tabs.
