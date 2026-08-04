// Cockpit bootstrap: session, streams, views and the slot clock.

// Kill any stale ServiceWorker that was caching old JS. This must run before
// anything else; index.html may not carry inline logic, so it lives here.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => r.unregister()));
}

import { api } from "./api.js";
import { applySnapshot, subscribe } from "./state.js";
import { startStreams } from "./streams.js";
import { createWaterfall, parseFrame } from "./waterfall.js";
import { createCandidates } from "./candidates.js";
import { createSafetyBar } from "./safety.js";
import { createBandSelect } from "./band.js";
import { createSettingsDrawer, loadSettings } from "./settings.js";

const loginView = document.getElementById("login-view");
const cockpit = document.getElementById("cockpit");
const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");

function showLogin(message = "") {
  loginView.hidden = false;
  cockpit.hidden = true;
  loginError.textContent = message;
}

async function boot() {
  const session = await api.currentSession();
  if (!session.ok) {
    showLogin();
    return;
  }
  loginView.hidden = true;
  cockpit.hidden = false;

  const waterfall = createWaterfall(document.getElementById("waterfall-canvas"));
  createCandidates(document.getElementById("candidate-list"));
  createSafetyBar();
  createBandSelect(document.getElementById("band-select"));
  createSettingsDrawer();
  // Seed FT8 display prefs before the first candidate render.
  applySnapshot({ settings: loadSettings() });

  const snapshot = await api.state();
  if (snapshot.ok) applySnapshot(snapshot);
  startStreams({
    onWaterfallFrame: (data) => {
      const frame = parseFrame(data);
      if (frame) waterfall.push(frame);
    },
  });

  subscribe(({ revision, safety, station }) => {
    document.getElementById("revision-badge").textContent = `r${revision}`;
    document.getElementById("station-id").textContent =
      `${(station && station.my_call) || "MRRC-FT8"}${safety.armed ? " — TX ARMED" : ""}`;
  });

  setInterval(() => {
    const now = new Date();
    document.getElementById("slot-clock").textContent =
      now.toISOString().slice(11, 19) + "Z";
  }, 250);
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = document.getElementById("login-password").value;
  const result = await api.login(password);
  if (result.ok) {
    document.getElementById("login-password").value = "";
    boot();
  } else if (result.reason === "rate_limited") {
    showLogin(`Too many attempts — wait ${Math.ceil(result.retry_after_s)} s`);
  } else {
    showLogin("Login failed");
  }
});

// ServiceWorker disabled during development — cache was serving stale JS
// if ("serviceWorker" in navigator) {
//   navigator.serviceWorker.register("/static/sw.js");
// }

boot();
