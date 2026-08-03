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
    activeTab = tab;
    for (const t of tabs) t.classList.toggle("active", t.dataset.tab === tab);
    renderTab(tab);
  }

  // ---- tab renderers ---------------------------------------------------

  async function renderRadio() {
    content.innerHTML = "<p class='drawer-hint'>Loading rig levels…</p>";
    let levels = {};
    let rigUp = true;
    const res = await api.rigLevels();
    if (res.ok) levels = res.levels || {};
    else rigUp = false;

    const rows = [
      { key: "ATT", label: "Attenuator", on: (levels.ATT ?? 0) > 0, toggle: true },
      { key: "PREAMP", label: "Preamp", on: (levels.PREAMP ?? 0) > 0, toggle: true },
      { key: "RF", label: "RF Gain", value: levels.RF, min: 0, max: 100, slider: true },
      { key: "AGC", label: "AGC", value: levels.AGC, min: 0, max: 100, slider: true },
    ];
    const html = [];
    html.push("<h3>Radio</h3>");
    if (!rigUp) {
      html.push("<p class='drawer-hint dim'>rigctld unreachable — levels unavailable</p>");
    }
    for (const r of rows) {
      if (r.toggle) {
        const on = r.on;
        html.push(`<label class="setting-row toggle">
          <span>${r.label}</span>
          <input type="checkbox" data-level="${r.key}" ${on ? "checked" : ""}
            ${rigUp ? "" : "disabled"}>
        </label>`);
      } else {
        const shown = r.value == null ? "—" : `${r.value}`;
        html.push(`<label class="setting-row">
          <span>${r.label} <b class="val">${shown}</b></span>
          <input type="range" data-level="${r.key}" min="${r.min}" max="${r.max}"
            value="${r.value ?? 0}" ${rigUp ? "" : "disabled"}>
        </label>`);
      }
    }
    html.push(`<div class="drawer-hint dim">Levels apply immediately to the rig.
      Unsupported items stay greyed out. TX must be off to change them.</div>`);
    content.innerHTML = html.join("");

    for (const input of content.querySelectorAll("input[data-level]")) {
      const level = input.dataset.level;
      input.addEventListener("change", async () => {
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

  async function renderLog() {
    content.innerHTML = "<p class='drawer-hint'>Loading QSO log…</p>";
    const res = await api.qsos();
    const qsos = res.ok ? res.qsos || [] : [];
    if (!res.ok) {
      content.innerHTML = `<p class='drawer-hint dim'>Could not load log: ${res.reason || res.status}</p>`;
      return;
    }
    const rows = qsos.map((q) => {
      const done = q.completed_epoch
        ? new Date(q.completed_epoch * 1000).toISOString().slice(0, 16).replace("T", " ")
        : "—";
      return `<div class="qso-row">
        <span class="qso-call">${q.dx_call}</span>
        <span class="qso-meta">${q.mode || ""} ${q.band || ""} ${done}</span>
      </div>`;
    }).join("");
    content.innerHTML = `
      <h3>Log <span class="count">${qsos.length}</span></h3>
      <a class="btn adif" href="/api/v1/logs/adif" download>Export ADIF</a>
      <div class="qso-list">${rows || "<p class='drawer-hint dim'>No QSOs yet.</p>"}</div>`;
  }

  function renderTab(tab) {
    if (tab === "radio") renderRadio();
    else if (tab === "ft8") renderFt8();
    else if (tab === "station") renderStation();
    else if (tab === "log") renderLog();
  }

  btnMenu.addEventListener("click", open);
  btnClose.addEventListener("click", close);
  backdrop.addEventListener("click", close);
  for (const t of tabs) t.addEventListener("click", () => switchTab(t.dataset.tab));

  // Reflect station info from fresh snapshots.
  subscribe(() => {
    if (activeTab === "station") renderStation();
    if (activeTab === "log") renderLog();
  });
}
