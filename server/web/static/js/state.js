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
  // FT8 messages repeat verbatim every slot; dedup only within the same
  // slot or the list freezes after the first slots and never scrolls.
  const key = (slotId, text) => `${slotId}:${text}`;
  const seen = new Set(state.candidates.map((c) => key(c.slot_id, c.text)));
  const additions = (batch.messages || [])
    .filter((m) => !seen.has(key(batch.slot_id, m.text)))
    .map((m) => ({ ...m, slot_id: batch.slot_id, late: batch.late }));
  patch({ candidates: [...additions, ...state.candidates].slice(0, 200) });
}
