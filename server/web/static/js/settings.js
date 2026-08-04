// Left settings drawer: Radio (rig levels/passband), FT8 (decode/display),
// Station (call & grid), Log (QSO history + ADIF).  FT8 preferences persist
// in localStorage and drive the candidate list filters/colours.

import { api } from "./api.js";
import { getState, patch, subscribe } from "./state.js";
import { showToast } from "./toast.js";

const STORAGE_KEY = "mrrc-ft8.settings";

// Defaults mirroring WSJT-X common values.
export const DEFAULTS = {
  decodeDepth: "fast",        // fast | deep
  colorScheme: "classic",     // classic | contrast | minimal
  showOnlyCQ: false,          // hide non-CQ rows
  hideWorked: false,          // hide calls already in the log (new-DXCC focus)
  hideMine: false,            // hide own echoes
};

export function loadSettings() {
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch { /* corrupt storage -> defaults */ }
  return { ...DEFAULTS, ...stored };
}

export function saveSettings(partial) {
  const next = { ...loadSettings(), ...partial };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  patch({ settings: next });
  return next;
}

// Rig mutations need the control lease; take it implicitly like candidate
// taps do (UC-002).  A lease held by another session stays rejected.
async function ensureLease() {
  if (getState().lease.mine) return true;
  if (getState().lease.held) return false;
  const acquired = await api.acquireLease();
  return Boolean(acquired.ok);
}

export function createSettingsDrawer() {
  const backdrop = document.getElementById("drawer-backdrop");
  const drawer = document.getElementById("settings-drawer");
  const content = document.getElementById("drawer-content");
  const tabs = Array.from(document.querySelectorAll("#drawer-tabs .tab"));
  const btnMenu = document.getElementById("btn-menu");
  const btnClose = document.getElementById("btn-drawer-close");
  let activeTab = "radio";

  // Seed the settings into state so the candidate list can filter.
  if (!getState().settings) patch({ settings: loadSettings() });

  function open() {
    backdrop.hidden = false;
    drawer.hidden = false;
    document.body.classList.add("drawer-open");
    renderTab(activeTab);
  }
  function close() {
    backdrop.hidden = true;
    drawer.hidden = true;
    document.body.classList.remove("drawer-open");
  }

  function switchTab(tab) {
    if (tab === "log") {
      close();          // leave the settings drawer
      openLogView();    // full-screen QSO log overlay
      return;
    }
    if (tab === "dxcc") {
      close();          // leave the settings drawer
      openDxccView();   // full-screen DXCC stats overlay
      return;
    }
    activeTab = tab;
    for (const t of tabs) t.classList.toggle("active", t.dataset.tab === tab);
    renderTab(tab);
  }

  // ---- tab renderers ---------------------------------------------------

  async function renderRadio() {
    content.innerHTML = "<p class='drawer-hint'>Loading rig settings…</p>";
    let mode = null;
    let rigUp = true;
    const levelRes = await api.rigLevels();
    // Sequential: both hit the same RigClient; concurrent requests interleave
    // with the level-query timeout/drop and corrupt the mode read (502).
    const modeRes = await api.rigMode();
    const levels = levelRes.ok ? levelRes.levels || {} : {};
    if (modeRes.ok) mode = modeRes;
    else rigUp = modeRes.status === 503 ? false : rigUp;

    // FT-710's hamlib model does not answer the ``L <name>`` query, so level
    // reads come back None — fall back to typical defaults; writes still work.
    const fallback = (key, def) => (levels[key] == null ? def : levels[key]);
    const agcMap = [
      [0, "OFF"], [2, "FAST"], [5, "MED"], [3, "SLOW"], [6, "AUTO"],
    ];
    const agcValue = fallback("AGC", 6);

    // FT-710 USB/LSB filter bandwidths (Hamlib rig model 1049).
    const passbands = [1800, 2400, 3000];
    const currentPassband = mode && mode.passband_hz;
    const currentMode = (mode && mode.mode) || "USB";
    const html = [];
    html.push("<h3>Radio</h3>");
    if (!rigUp) {
      html.push("<p class='drawer-hint dim'>rigctld unreachable — controls unavailable</p>");
    }

    // Filter bandwidth selector (raw CAT SH via /radio/filter; hamlib's
    // M-passband path is broken on the FT-710 with hamlib 4.6.2).
    html.push(`<label class="setting-row">
      <span>Mode</span>
      <b>${currentMode}</b>
    </label>`);
    html.push(`<label class="setting-row">
      <span>Filter bandwidth</span>
      <select data-mode-passband ${rigUp ? "" : "disabled"}>
        ${passbands.map((hz) =>
          `<option value="${hz}" ${hz === currentPassband ? "selected" : ""}>
            ${(hz / 1000).toFixed(1)} kHz</option>`
        ).join("")}
      </select>
    </label>`);

    // ATT: FT-710 attenuator is 6/12/18 dB (not a plain on/off).
    const attValue = fallback("ATT", 0);
    html.push(`<label class="setting-row">
      <span>Attenuator</span>
      <select data-level="ATT" ${rigUp ? "" : "disabled"}>
        ${[0, 6, 12, 18].map((db) =>
          `<option value="${db}" ${db === attValue ? "selected" : ""}>${db ? `${db} dB` : "Off"}</option>`
        ).join("")}
      </select>
    </label>`);

    // PREAMP: 10/20 dB (0 = off).
    const preampValue = fallback("PREAMP", 0);
    html.push(`<label class="setting-row">
      <span>Preamp</span>
      <select data-level="PREAMP" ${rigUp ? "" : "disabled"}>
        ${[0, 10, 20].map((db) =>
          `<option value="${db}" ${db === preampValue ? "selected" : ""}>${db ? `${db} dB` : "Off"}</option>`
        ).join("")}
      </select>
    </label>`);

    // AGC: FT-710 discrete modes (0=OFF 2=FAST 5=MED 3=SLOW 6=AUTO).
    html.push(`<label class="setting-row">
      <span>AGC</span>
      <select data-level="AGC" ${rigUp ? "" : "disabled"}>
        ${agcMap.map(([v, label]) =>
          `<option value="${v}" ${v === agcValue ? "selected" : ""}>${label}</option>`
        ).join("")}
      </select>
    </label>`);

    // RF Gain: 0..1.0.
    const rfValue = fallback("RF", 1.0);
    html.push(`<label class="setting-row">
      <span>RF Gain <b class="val">${rfValue == null ? "—" : Math.round(rfValue * 100)}%</b></span>
      <input type="range" data-level="RF" min="0" max="1" step="0.01"
        value="${rfValue ?? 1}" ${rigUp ? "" : "disabled"}>
    </label>`);

    html.push(`<div class="drawer-hint dim">Changes apply immediately.
      TX must be off. Unsupported items stay greyed out.</div>`);
    content.innerHTML = html.join("");

    const passbandSelect = content.querySelector("[data-mode-passband]");
    if (passbandSelect) {
      passbandSelect.addEventListener("change", async () => {
        const ok = await ensureLease();
        if (!ok) {
          showToast("Control is held by another session");
          return;
        }
        const hz = Number(passbandSelect.value);
        const result = await api.rigFilter(hz);
        if (!result.ok) {
          // Fallback: the mode-set path applies the width too (best effort).
          const fallback = await api.rigModeSet(currentMode, hz);
          if (!fallback.ok) {
            showToast(`Filter: ${result.status} — ${result.reason || "unavailable"}`);
          } else {
            showToast(`Filter → ${(hz / 1000).toFixed(1)} kHz`);
          }
        } else {
          showToast(`Filter → ${(hz / 1000).toFixed(1)} kHz`);
        }
        // Read back so the drawer reflects the rig's actual passband.
        const modeRes = await api.rigMode();
        if (modeRes.ok && modeRes.passband_hz) {
          passbandSelect.value = String(modeRes.passband_hz);
        }
      });
    }
    for (const input of content.querySelectorAll("select[data-level], input[data-level]")) {
      const level = input.dataset.level;
      input.addEventListener("change", async () => {
        const ok = await ensureLease();
        if (!ok) {
          showToast("Control is held by another session");
          return;
        }
        const value = input.type === "checkbox" ? (input.checked ? 1 : 0) : Number(input.value);
        const result = await api.rigLevel(level, value);
        if (!result.ok) showToast(`Rig ${level}: ${result.reason || result.status}`);
        else showToast(`${level} → ${value}`);
      });
    }
  }

  function renderFt8() {
    const s = loadSettings();
    content.innerHTML = `
      <h3>FT8</h3>
      <label class="setting-row">
        <span>Decode depth</span>
        <select data-setting="decodeDepth">
          <option value="fast" ${s.decodeDepth === "fast" ? "selected" : ""}>Fast</option>
          <option value="deep" ${s.decodeDepth === "deep" ? "selected" : ""}>Deep</option>
        </select>
      </label>
      <label class="setting-row">
        <span>Colour scheme</span>
        <select data-setting="colorScheme">
          <option value="classic" ${s.colorScheme === "classic" ? "selected" : ""}>Classic</option>
          <option value="contrast" ${s.colorScheme === "contrast" ? "selected" : ""}>High contrast</option>
          <option value="minimal" ${s.colorScheme === "minimal" ? "selected" : ""}>Minimal</option>
        </select>
      </label>
      <label class="setting-row toggle">
        <span>Show only CQ calls</span>
        <input type="checkbox" data-setting="showOnlyCQ" ${s.showOnlyCQ ? "checked" : ""}>
      </label>
      <label class="setting-row toggle">
        <span>Hide already-worked calls</span>
        <input type="checkbox" data-setting="hideWorked" ${s.hideWorked ? "checked" : ""}>
      </label>
      <label class="setting-row toggle">
        <span>Hide my own echoes</span>
        <input type="checkbox" data-setting="hideMine" ${s.hideMine ? "checked" : ""}>
      </label>
      <div class="drawer-hint dim">These display preferences are stored in this
        browser and apply immediately to the Band Activity list.</div>`;
    for (const input of content.querySelectorAll("[data-setting]")) {
      input.addEventListener("change", () => {
        const value = input.type === "checkbox" ? input.checked : input.value;
        saveSettings({ [input.dataset.setting]: value });
      });
    }
  }

  function renderStation() {
    const { station } = getState();
    const call = station?.my_call || "—";
    const grid = station?.my_grid || "—";
    const worked = station?.worked_calls || [];
    content.innerHTML = `
      <h3>Station</h3>
      <div class="setting-row"><span>Call sign</span><b>${call}</b></div>
      <div class="setting-row"><span>Grid square</span><b>${grid}</b></div>
      <div class="setting-row"><span>Worked calls</span><b>${worked.length}</b></div>
      <div class="drawer-hint dim">Call &amp; grid are configured on the server
        (MRRC_FT8_MY_CALL / MRRC_FT8_MY_GRID). Worked calls come from the QSO
        log and drive the “hide already-worked” filter.</div>`;
  }

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

  // ---- full-screen DXCC stats ------------------------------------------

  const dxccOverlay = document.getElementById("dxcc-overlay");
  const dxccContent = document.getElementById("dxcc-content");
  const dxccCount = document.getElementById("dxcc-count");

  async function openDxccView() {
    dxccOverlay.hidden = false;
    document.body.classList.add("log-open");
    dxccContent.innerHTML = "<p class='drawer-hint'>Loading DXCC stats…</p>";
    const res = await api.dxcc();
    if (!res.ok) {
      dxccContent.innerHTML =
        `<p class='drawer-hint dim'>Could not load DXCC: ${res.reason || res.status}</p>`;
      return;
    }
    if (dxccCount) dxccCount.textContent = String(res.total);
    const html = [];
    html.push(`<div class="drawer-hint">已通联 <b>${res.total}</b> 个 DXCC 实体` +
      (res.unmatched ? `（${res.unmatched} 条未识别呼号）` : "") + "</div>");
    // 波段矩阵（by_band 降序）
    const bands = Object.entries(res.by_band || {})
      .sort((a, b) => b[1] - a[1]);
    if (bands.length) {
      html.push("<h3>By band</h3>");
      html.push(bands.map(([band, n]) =>
        `<div class="qso-row"><span class="qso-call">${band}</span>` +
        `<span class="qso-meta">${n} DXCC</span></div>`).join(""));
    }
    // 实体列表
    html.push("<h3>Entities</h3>");
    const rows = (res.entities || []).map((e) =>
      `<div class="qso-row"><span class="qso-call">${e.name}</span>` +
      `<span class="qso-meta">${e.continent} · ${e.first_utc} · ${e.band_count} band(s)</span></div>`
    ).join("");
    html.push(rows || "<p class='drawer-hint dim'>No DXCC yet.</p>");
    dxccContent.innerHTML = html.join("");
  }

  function closeDxccView() {
    dxccOverlay.hidden = true;
    document.body.classList.remove("log-open");
  }

  function renderTab(tab) {
    if (tab === "radio") renderRadio();
    else if (tab === "ft8") renderFt8();
    else if (tab === "station") renderStation();
  }

  btnMenu.addEventListener("click", open);
  btnClose.addEventListener("click", close);
  backdrop.addEventListener("click", close);
  for (const t of tabs) t.addEventListener("click", () => switchTab(t.dataset.tab));

  document.getElementById("btn-log-close").addEventListener("click", closeLogView);
  logOverlay.addEventListener("click", (event) => {
    // Clicking the dimmed backdrop (outside the panel) closes the overlay.
    if (event.target === logOverlay) closeLogView();
  });

  document.getElementById("btn-dxcc-close").addEventListener("click", closeDxccView);
  dxccOverlay.addEventListener("click", (event) => {
    // Clicking the dimmed backdrop (outside the panel) closes the overlay.
    if (event.target === dxccOverlay) closeDxccView();
  });

  // Reflect station info from fresh snapshots.
  subscribe(() => {
    if (activeTab === "station") renderStation();
  });
}
