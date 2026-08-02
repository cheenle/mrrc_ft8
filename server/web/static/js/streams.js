// Three bounded stream clients with reconnect backoff (§10.2).

import { applyDecodeBatch, applySnapshot, patch } from "./state.js";

const BACKOFF_MS = [500, 1000, 2000, 5000];
const connected = { state: false, decodes: false, waterfall: false };

function setConnected(name, value) {
  connected[name] = value;
  patch({ connected: { ...connected } });
}

function connect(path, { binary = false, onMessage, name }) {
  let attempt = 0;
  let socket = null;
  let closed = false;

  function open() {
    if (closed) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}${path}`);
    if (binary) socket.binaryType = "arraybuffer";

    socket.onopen = () => {
      attempt = 0;
      setConnected(name, true);
    };
    socket.onmessage = (event) => onMessage(event.data);
    socket.onclose = () => {
      setConnected(name, false);
      if (closed) return;
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      setTimeout(open, delay);
    };
  }

  open();
  return {
    close: () => {
      closed = true;
      if (socket) socket.close();
    },
  };
}

export function startStreams({ onWaterfallFrame }) {
  connect("/ws/v1/state", {
    name: "state",
    onMessage: (data) => applySnapshot(JSON.parse(data)),
  });
  connect("/ws/v1/decodes", {
    name: "decodes",
    onMessage: (data) => {
      const batch = JSON.parse(data);
      if (batch.type === "decodes") applyDecodeBatch(batch);
    },
  });
  connect("/ws/v1/waterfall", {
    name: "waterfall",
    binary: true,
    onMessage: (data) => onWaterfallFrame(data),
  });
}
