# QSO Log Full-Screen Overlay — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Log" menu item in the cockpit settings drawer that opens a full-screen overlay listing the QSO log (call / mode+band / completed UTC) with ADIF export.

**Architecture:** Approach A — all logic lives in the existing `server/web/static/js/settings.js`. `index.html` gains a hidden full-screen `#log-overlay`; `app.css` gains overlay styles reusing the existing design tokens; no backend change (`GET /api/v1/logs/qsos` and `/api/v1/logs/adif` already exist).

**Tech Stack:** Vanilla JS ES modules, HTML5, CSS3. No build step. No JS test framework (per spec §6, frontend verified manually; backend API already covered by pytest).

## Global Constraints

- Follow the existing SPA module style in `server/web/static/js/` (small focused files, `api.` client for fetches, `.qso-row` / `.qso-call` / `.qso-meta` / `.btn.adif` / `.drawer-hint` classes).
- CSS reuses existing design tokens only: `--bg` `--panel` `--edge` `--text` `--dim` `--accent` (from `app.css:3-10`).
- No backend changes, no new JS files, no new static routes.
- The overlay is DOM-only state: opening/closing it must never touch the rig, sequencer, lease, or session.
- ADIF export stays a plain `<a href="/api/v1/logs/adif" download>` (session cookie, same-origin).
- Static files are served live from disk by FastAPI `StaticFiles`; no server restart is needed, but the browser may need a hard refresh (Ctrl/Cmd+Shift+R) to drop cached JS/CSS/HTML.

---
## File Structure

- `server/web/static/index.html` — add the hidden full-screen `#log-overlay` block after `#settings-drawer` (index.html:59).
- `server/web/static/css/app.css` — append `.log-overlay`, `.log-panel`, `.log-header`, `.log-list` styles at the end of the file.
- `server/web/static/js/settings.js` — special-case the `log` tab in `switchTab`, add `openLogView` / `closeLogView`, wire the close button + backdrop click, and remove the inline `renderLog`.
- `docs/superpowers/specs/2026-08-04-log-page-design.md` — the governing spec (already committed).

---

### Task 1: Overlay markup + styles

**Files:**
- Modify: `server/web/static/index.html` (after line 59, the `</aside>` of `#settings-drawer`)
- Modify: `server/web/static/css/app.css` (append at end)

**Interfaces:**
- Produces: `#log-overlay` (hidden by default), `#log-list`, `#btn-log-close`, `#log-count` — referenced by Task 2.

- [ ] **Step 1: Add the overlay markup to `index.html`**

Insert immediately after the `#settings-drawer` `</aside>` block (after line 59), before the `<div id="toast">`:

```html
    <!-- Full-screen QSO log -->
    <div id="log-overlay" class="log-overlay" hidden>
      <div class="log-panel">
        <header class="log-header">
          <strong>QSO Log <span class="count" id="log-count"></span></strong>
          <a class="btn adif" href="/api/v1/logs/adif" download>Export ADIF</a>
          <button id="btn-log-close" class="icon-btn" aria-label="Close log">✕</button>
        </header>
        <div id="log-list" class="log-list"></div>
      </div>
    </div>
```

- [ ] **Step 2: Append the overlay styles to `app.css`**

```css
/* Full-screen QSO log overlay */
.log-overlay {
  position: fixed; inset: 0; z-index: 1000; background: rgba(0, 0, 0, 0.65);
  display: flex; align-items: center; justify-content: center; padding: 1.5rem;
}
.log-panel {
  width: min(880px, 100%); max-height: 88vh; background: var(--panel);
  border: 1px solid var(--edge); border-radius: 12px;
  display: flex; flex-direction: column; overflow: hidden;
}
.log-header {
  display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  border-bottom: 1px solid var(--edge);
}
.log-header strong { flex: 1; }
.log-list { overflow-y: auto; padding: 6px 16px 16px; }
.log-list .qso-row { font-size: 14px; padding: 8px 2px; }
```

- [ ] **Step 3: Verify the page still loads and the overlay is hidden**

Open `http://127.0.0.1:8000/static/index.html` (or the live cockpit URL) with a hard refresh. Expected: the cockpit renders normally; `#log-overlay` exists in the DOM with `hidden` attribute; the drawer and waterfall/candidates are unaffected.

- [ ] **Step 4: Commit**

```bash
git add server/web/static/index.html server/web/static/css/app.css
git commit -m "feat(web): full-screen QSO log overlay markup + styles"
```

---

### Task 2: Log overlay behaviour in settings.js

**Files:**
- Modify: `server/web/static/js/settings.js` — `switchTab` (currently ~line 68), tab wiring (`for (const t of tabs) ...` ~line 299), and the `renderLog` function (~line 266) to be removed.

**Interfaces:**
- Consumes: `#log-overlay`, `#log-list`, `#btn-log-close`, `#log-count` (from Task 1); `api.qsos()` from `server/web/static/js/api.js`; `close()` (the drawer's existing close function inside `createSettingsDrawer`).
- Produces: `openLogView()` / `closeLogView()` (closure functions inside `createSettingsDrawer`, no module exports needed).

- [ ] **Step 1: Special-case the `log` tab in `switchTab`**

Change the existing `switchTab` so that choosing `log` closes the drawer and opens the overlay instead of rendering inline:

```js
  function switchTab(tab) {
    if (tab === "log") {
      close();          // leave the settings drawer
      openLogView();    // full-screen QSO log overlay
      return;
    }
    activeTab = tab;
    for (const t of tabs) t.classList.toggle("active", t.dataset.tab === tab);
    renderTab(tab);
  }
```

- [ ] **Step 2: Add `openLogView` / `closeLogView` and remove the inline `renderLog`**

Replace the entire `async function renderLog() { ... }` block with:

```js
  // ---- full-screen QSO log -------------------------------------------

  const logOverlay = document.getElementById("log-overlay");
  const logList = document.getElementById("log-list");
  const logCount = document.getElementById("log-count");

  async function openLogView() {
    logOverlay.hidden = false;
    document.body.classList.add("log-open");
    logList.innerHTML = "<p class='drawer-hint'>Loading QSO log…</p>";
    const res = await api.qsos();
    if (!res.ok) {
      logList.innerHTML =
        `<p class='drawer-hint dim'>Could not load log: ${res.reason || res.status}</p>`;
      return;
    }
    const qsos = res.qsos || [];
    if (logCount) logCount.textContent = String(qsos.length);
    const rows = qsos.map((q) => {
      const done = q.completed_epoch
        ? new Date(q.completed_epoch * 1000).toISOString().slice(0, 16).replace("T", " ")
        : "—";
      return `<div class="qso-row">
        <span class="qso-call">${q.dx_call}</span>
        <span class="qso-meta">${q.mode || ""} ${q.band || ""} ${done}</span>
      </div>`;
    }).join("");
    logList.innerHTML = rows || "<p class='drawer-hint dim'>No QSOs yet.</p>";
  }

  function closeLogView() {
    logOverlay.hidden = true;
    document.body.classList.remove("log-open");
  }
```

- [ ] **Step 3: Wire the close button and backdrop click**

In `createSettingsDrawer`, after the existing `for (const t of tabs) ...` wiring, add:

```js
  document.getElementById("btn-log-close").addEventListener("click", closeLogView);
  logOverlay.addEventListener("click", (event) => {
    // Clicking the dimmed backdrop (outside the panel) closes the overlay.
    if (event.target === logOverlay) closeLogView();
  });
```

- [ ] **Step 4: Remove the `log` branch from `renderTab`**

The existing `renderTab` dispatches `else if (tab === "log") renderLog();` — remove that branch (renderLog no longer exists):

```js
  function renderTab(tab) {
    if (tab === "radio") renderRadio();
    else if (tab === "ft8") renderFt8();
    else if (tab === "station") renderStation();
  }
```

- [ ] **Step 5: Verify behaviour on the running app**

Hard-refresh the cockpit, log in, open the ☰ drawer. Expected:
- Clicking **Log** closes the drawer and shows the full-screen overlay.
- The overlay lists real QSOs as `.qso-row` rows (call on the left, `mode band YYYY-MM-DD HH:MM` on the right) with the count in the header.
- **Export ADIF** downloads the ADIF file.
- Clicking **✕** or the dimmed backdrop closes the overlay and the cockpit (waterfall/candidates) is intact behind it.
- With no QSOs present (fresh repo/db), the overlay shows "No QSOs yet.".
- With the server stopped or the session expired, the overlay shows "Could not load log: …".

- [ ] **Step 6: Commit**

```bash
git add server/web/static/js/settings.js
git commit -m "feat(web): drawer Log opens full-screen QSO log overlay"
```

---

### Task 3: End-to-end verification + docs

**Files:**
- Test: run the backend suite and re-check the overlay against the spec.

- [ ] **Step 1: Run the full test suite (backend unchanged but confirm green)**

Run: `venv/bin/python -m pytest -q`
Expected: all tests pass (no backend change; confirms nothing regressed).

- [ ] **Step 2: Verify spec coverage against the live overlay**

Walk the spec §3.1–§3.3 and §5 one more time against the running UI:
- Overlay opens from the drawer Log menu item; drawer closes. ✓
- Simple list shows call / mode+band / completed UTC; count in header. ✓
- ADIF export link downloads. ✓
- Close via ✕ and backdrop click. ✓
- Error state (`Could not load log: …`) and empty state (`No QSOs yet.`) render. ✓
- Opening/closing never touches rig/sequencer/lease (watch the state bar during open/close). ✓

- [ ] **Step 3: Commit any fix-ups**

If a fix-up was needed, commit it:

```bash
git add -A
git commit -m "fix(web): QSO log overlay adjustments"
```

---
## Self-Review

- **Spec coverage:** §3.1 markup → Task 1; §3.2 settings.js logic → Task 2; §3.3 CSS → Task 1; §4 data flow → Task 2; §5 error handling → Task 2 (`res.ok` branch + empty hint); §6 testing → Task 3; §7 non-goals respected (no new page/routing/search/backend change).
- **Placeholder scan:** no TBD/TODO; every code step carries concrete code.
- **Type consistency:** element IDs (`#log-overlay`, `#log-list`, `#btn-log-close`, `#log-count`) and function names (`openLogView`, `closeLogView`, `switchTab`, `renderTab`) are consistent across Task 1 and Task 2.
