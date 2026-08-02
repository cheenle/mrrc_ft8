// Band Activity list: compact WSJT-X-style decode rows.

import { api } from "./api.js";
import { getState, patch, subscribe } from "./state.js";

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
  function render() {
    const { candidates, selected } = getState();
    listElement.replaceChildren(
      ...candidates.map((candidate) => {
        const item = document.createElement("li");
        item.className = "candidate";
        if (candidate.is_cq) item.classList.add("cq");
        if (candidate.to_me) item.classList.add("to-me");
        if (candidate.late) item.classList.add("late");
        if (selected && selected.call === candidate.call) item.classList.add("selected");
        item.textContent = rowText(candidate);
        item.addEventListener("click", () => select(candidate));
        item.addEventListener("dblclick", () => reply(candidate));
        return item;
      }),
    );
  }

  async function select(candidate) {
    // Selecting never arms or transmits; it only enables the Reply button.
    const result = await api.select(candidate);
    if (result.ok) {
      patch({ selected: { call: candidate.call, grid: candidate.grid || "" } });
    }
  }

  async function reply(candidate) {
    // Double-click = select + Reply through the exact same gated paths.
    const chosen = await api.select(candidate);
    if (chosen.ok) {
      patch({ selected: { call: candidate.call, grid: candidate.grid || "" } });
      await api.reply();
    }
  }

  subscribe(render);
  render();
}
