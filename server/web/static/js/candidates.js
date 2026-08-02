// Band Activity list: compact WSJT-X-style decode rows.

import { api } from "./api.js";
import { getState, patch, subscribe } from "./state.js";
import { showToast } from "./toast.js";

function slotUtc(slotId) {
  return new Date(slotId * 15_000).toISOString().slice(11, 19);
}

function rowText(c) {
  const snr = `${c.snr > 0 ? "+" : ""}${c.snr}`.padStart(4);
  const dt = `${c.dt >= 0 ? "+" : ""}${Number(c.dt).toFixed(1)}`.padStart(5);
  const freq = String(Math.round(c.freq)).padStart(5);
  return `${slotUtc(c.slot_id)} ${snr} ${dt} ${freq} ${c.text}`;
}

export function createCandidates(listElement) {
  const STALE_AFTER_MS = 10 * 60_000;

  function separatorText(slotId, freqHz) {
    const utc = slotUtc(slotId);
    return freqHz
      ? `── ${(freqHz / 1e6).toFixed(3)} MHz ─ ${utc} UTC ──`
      : `── ${utc} UTC ──`;
  }

  function render() {
    const { candidates, selected, radio } = getState();
    const now = Date.now();
    const freqHz = radio && radio.freq_hz;
    const items = [];
    let previousSlot = null;
    for (const candidate of candidates) {
      if (candidate.slot_id !== previousSlot) {
        previousSlot = candidate.slot_id;
        const separator = document.createElement("li");
        separator.className = "separator";
        separator.textContent = separatorText(candidate.slot_id, freqHz);
        items.push(separator);
      }
      const item = document.createElement("li");
      item.className = "candidate";
      if (candidate.is_cq) item.classList.add("cq");
      if (candidate.to_me) item.classList.add("to-me");
      if (candidate.late) item.classList.add("late");
      if (now - (candidate._t || 0) > STALE_AFTER_MS) item.classList.add("stale");
      if (selected && selected.call === candidate.call) item.classList.add("selected");
      item.textContent = rowText(candidate);
      item.addEventListener("click", () => select(candidate));
      item.addEventListener("dblclick", () => reply(candidate));
      items.push(item);
    }
    listElement.replaceChildren(...items);
  }

  async function select(candidate) {
    // Selecting never arms or transmits; it only enables the Reply button.
    let result = await api.select(candidate);
    if (!result.ok && result.reason === "lease_required" && !getState().lease.held) {
      // WSJT-X-style single tap: a free control lease is taken implicitly so
      // the tap just works. A lease held by another session still rejects —
      // exactly one controller at a time (§10.3, UC-002).
      const acquired = await api.acquireLease();
      result = acquired.ok ? await api.select(candidate) : acquired;
    }
    if (result.ok) {
      patch({ selected: { call: candidate.call, grid: candidate.grid || "" } });
      return true;
    }
    showToast(
      result.reason === "lease_required"
        ? "Control is held by another session"
        : `Select rejected: ${result.reason || result.status}`,
    );
    return false;
  }

  async function reply(candidate) {
    // Double-click = select + Reply through the exact same gated paths.
    if (await select(candidate)) {
      const result = await api.reply();
      if (!result.ok) showToast(`Reply rejected: ${result.reason || result.status}`);
    }
  }

  subscribe(render);
  // Re-render on a slow tick so quiet-band rows visibly age into "stale".
  setInterval(render, 30_000);
  render();
}
