// Three bounded WebSocket streams from the MRRC-FT8 server, with reconnect
// backoff.  Decodes and state are JSON; waterfall is binary WF01 frames
// (magic/seq/epoch_ms/bin_hz/count + one byte per frequency bin).

export interface ServerDecodeMessage {
  text: string;
  snr: number;
  dt: number;
  freq: number;
  call: string;
  grid: string;
  is_cq: boolean;
  to_me: boolean;
  mine: boolean;
  is_new_dxcc: boolean;
  entity: string;
}

export interface ServerDecodeBatch {
  slot_id: number;
  late: boolean;
  messages: ServerDecodeMessage[];
}

export interface WaterfallFrame {
  seq: number;
  epochMs: number;
  binHz: number;
  bins: Uint8Array;
}

const WF_HEADER = 22; // "<4sIQfH": magic, seq, epoch_ms, bin_hz, count
const AUTH_FAILURE_CLOSE = 4401;
const BACKOFF_MS = [500, 1000, 2000, 5000];

export function parseFrame(buffer: ArrayBuffer): WaterfallFrame | null {
  if (buffer.byteLength < WF_HEADER) return null;
  const view = new DataView(buffer);
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
  if (magic !== 'WF01') return null;
  const count = view.getUint16(20, true);
  if (buffer.byteLength !== WF_HEADER + count) return null;
  return {
    seq: view.getUint32(4, true),
    epochMs: Number(view.getBigUint64(8, true)),
    binHz: view.getFloat32(16, true),
    bins: new Uint8Array(buffer, WF_HEADER, count),
  };
}

interface StreamOptions {
  path: string;
  binary?: boolean;
  onMessage: (data: any) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onAuthFailure?: () => void;
}

export interface StreamHandle { close: () => void; }

function connect({ path, binary = false, onMessage, onOpen, onClose, onAuthFailure }: StreamOptions): StreamHandle {
  let attempt = 0;
  let socket: WebSocket | null = null;
  let closed = false;

  const open = () => {
    if (closed) return;
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${scheme}://${location.host}${path}`);
    if (binary) socket.binaryType = 'arraybuffer';

    socket.onopen = () => { attempt = 0; onOpen?.(); };
    socket.onmessage = (event) => onMessage(event.data);
    socket.onclose = (event) => {
      onClose?.();
      if (closed) return;
      if (event.code === AUTH_FAILURE_CLOSE) { onAuthFailure?.(); return; }
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      setTimeout(open, delay);
    };
  };
  open();
  return {
    close: () => { closed = true; if (socket) socket.close(); },
  };
}

export interface StreamSet {
  state: StreamHandle;
  decodes: StreamHandle;
  waterfall: StreamHandle;
}

export interface StreamCallbacks {
  onState: (snapshot: Record<string, any>) => void;
  onDecodes: (batch: ServerDecodeBatch) => void;
  onWaterfall: (frame: WaterfallFrame) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onAuthFailure?: () => void;
}

export function startStreams(cb: StreamCallbacks): StreamSet {
  const onAuthFailure = cb.onAuthFailure;
  return {
    state: connect({
      path: '/ws/v1/state',
      onMessage: (data) => { try { cb.onState(JSON.parse(data)); } catch { /* ignore */ } },
      onOpen: cb.onOpen,
      onClose: cb.onClose,
      onAuthFailure,
    }),
    decodes: connect({
      path: '/ws/v1/decodes',
      onMessage: (data) => {
        try {
          const batch = JSON.parse(data);
          if (batch.type === 'decodes') cb.onDecodes(batch as ServerDecodeBatch);
        } catch { /* ignore */ }
      },
      onAuthFailure,
    }),
    waterfall: connect({
      path: '/ws/v1/waterfall',
      binary: true,
      onMessage: (data) => {
        const frame = parseFrame(data as ArrayBuffer);
        if (frame) cb.onWaterfall(frame);
      },
      onAuthFailure,
    }),
  };
}
