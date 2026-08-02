// Cockpit bootstrap: session, streams, views and the slot clock.

import { api } from "./api.js";
import { applySnapshot, subscribe } from "./state.js";
import { startStreams } from "./streams.js";
import { createWaterfall, parseFrame } from "./waterfall.js";
import { createCandidates } from "./candidates.js";
import { createSafetyBar } from "./safety.js";

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

  const snapshot = await api.state();
  if (snapshot.ok) applySnapshot(snapshot);
  startStreams({
    onWaterfallFrame: (data) => {
      const frame = parseFrame(data);
      if (frame) waterfall.push(frame);
    },
  });

  subscribe(({ revision, safety }) => {
    document.getElementById("revision-badge").textContent = `r${revision}`;
    document.getElementById("station-id").textContent =
      `${snapshot.station || "MRRC-FT8"}${safety.armed ? " — TX ARMED" : ""}`;
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

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js");
}

boot();
