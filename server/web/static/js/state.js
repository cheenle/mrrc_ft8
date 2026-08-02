// Tiny client state store: streams and REST intents write, views subscribe.

const state = {
  revision: 0,
  lease: { held: false, mine: false },
  safety: { armed: false, ptt_on: false, faults: [] },
  sequencer: { state: "idle", tx_enabled: false, dx_call: "" },
  selected: null,
  candidates: [],
  connected: { state: false, decodes: false, waterfall: false },
};

const listeners = new Set();

export function getState() {
  return state;
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function patch(fragment) {
  Object.assign(state, fragment);
  for (const listener of listeners) listener(state);
}

export function applySnapshot(snapshot) {
  patch({
    revision: snapshot.revision ?? state.revision,
    lease: snapshot.lease ?? state.lease,
    safety: snapshot.safety ?? state.safety,
    sequencer: snapshot.sequencer ?? state.sequencer,
    selected: snapshot.selected ?? state.selected,
  });
}

export function applyDecodeBatch(batch) {
  // WSJT-X style: one row per callsign, newest decode updates the entry.
  const seen = new Set();
  const updated = new Map();
  const now = Date.now();
  for (const m of batch.messages || []) {
    const key = m.call || m.text;
    if (seen.has(key)) continue;
    seen.add(key);
    updated.set(key, { ...m, slot_id: batch.slot_id, late: batch.late, _t: now });
  }
  const kept = state.candidates.filter(c => !updated.has(c.call || c.text));
  const merged = [...updated.values(), ...kept]
    .sort((a, b) => (b._t || 0) - (a._t || 0))
    .slice(0, 200);
  patch({ candidates: merged });
}
