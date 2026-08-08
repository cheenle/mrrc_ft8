# Desktop Client — FT8web Brain Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adapt the FT8web React client into a desktop FT8 interface that talks to the MRRC-FT8 headless server instead of doing in-browser DSP/CAT — keeping FT8web's UI shell (waterfall, Band Activity, Active QSO, settings, logbook).

**Architecture:** New server-adapter modules (`mrrcClient.ts`, `mrrcStreams.ts`, `useServerFT8.ts`) replace FT8web's audio/worker/CAT/FSM brain. `App.tsx` keeps its JSX/layout but its data sources are rewired to the server's REST + 3 WebSockets. The build is served at `/desktop` by the MRRC-FT8 server (see the companion plan `2026-08-08-desktop-server-additions.md`).

**Tech Stack:** React 19, TypeScript, Vite 6, Tailwind v4, vitest. Node ≥ 20.

## Global Constraints

- **Do not touch** `server/web/static/` (the mobile PWA) or any existing server contract.
- Line numbers in App.tsx tasks are **approximate anchors** — locate each target by the function/identifier named, not by line number alone (the file is 2855 lines and shifts as you edit).
- Keep the exact `FT8DecodedMessage` interface (already exported from `App.tsx`) — the render code depends on its fields.
- Every `App.tsx` task ends with `npm run lint` (tsc) and `npm run build` passing.
- Vitest tests live in `src/**/*.test.ts` (config: `environment: 'node'`).
- Verify against a running server: `OMP_STACKSIZE=10M venv/bin/python -m server.main` (loopback :8000).

---

### Task 1: Scaffold — copy FT8web, gitignore, install, baseline build

**Files:**
- Create: `desktop/ft8web/` (copy of `FT8web/` minus its `.git`)
- Modify: `.gitignore`

**Interfaces:**
- Produces: a working `desktop/ft8web/` tree that `npm install` + `npm run build` succeeds on **unchanged** FT8web code (baseline before any adaptation).

- [ ] **Step 1: Copy and de-git**

```bash
cd /Users/cheenle/HAM/ft8
rsync -a --exclude='.git' --exclude='node_modules' FT8web/ desktop/ft8web/
# remove any nested .git artifacts that rsync might have kept
rm -rf desktop/ft8web/.git
ls desktop/ft8web/src/ | head
test -f desktop/ft8web/LICENSE && echo "LICENSE preserved (GPL v3)"
```

- [ ] **Step 2: .gitignore the pristine clone and the build artifacts**

Append to `.gitignore`:

```gitignore
# Desktop client reference clone (pristine; do not commit)
FT8web/
# Desktop client build artifacts
desktop/ft8web/node_modules/
desktop/ft8web/dist/
```

- [ ] **Step 3: Baseline install + build**

```bash
cd /Users/cheenle/HAM/ft8/desktop/ft8web
npm install
npm run build
```

Expected: `vite build` succeeds and writes `dist/`. If `@e04/ft8ts` (github dep) fails to install, it is only needed for the local DSP we are removing — proceed after noting it; Task 2 removes the import and later tasks may drop it from `package.json`.

- [ ] **Step 4: Drop the PWA service worker + Eruda for the desktop client**

In `desktop/ft8web/src/main.tsx`, remove the Service Worker registration and Eruda debug console blocks (keep only `createRoot(...).render(<App />)`). This avoids an offline SW caching `/desktop` assets with the wrong scope and breaking the no-cache API/WS contract.

- [ ] **Step 5: Commit**

Repository-concurrency note: another process may be committing to this branch in the same working tree — stage ONLY your paths, never `git add -u`/`-A`/`.`.

```bash
cd /Users/cheenle/HAM/ft8
git add .gitignore desktop/ft8web/
git commit -m "chore(desktop): scaffold ft8web fork at desktop/ft8web"
```

---

### Task 2: `mrrcClient.ts` — typed REST client

**Files:**
- Create: `desktop/ft8web/src/services/mrrcClient.ts`
- Test: `desktop/ft8web/src/services/mrrcClient.test.ts`

**Interfaces:**
- Produces: `mrrc` object (all server `/api/v1` calls), `key()`, `setUnauthorizedHandler(fn)`, `currentRevision()`. Auth failure (HTTP 401 outside `/session/`) invokes the unauthorized handler (the app shows the login view).

- [ ] **Step 1: Write the failing tests**

`desktop/ft8web/src/services/mrrcClient.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from 'vitest';
import { mrrc, setUnauthorizedHandler } from './mrrcClient';

function mockFetch(status: number, payload: Record<string, unknown> = {}) {
  return vi.fn(async () =>
    new Response(JSON.stringify({ ok: status < 400, ...payload }), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  ) as unknown as typeof fetch;
}

describe('mrrcClient', () => {
  afterEach(() => { vi.restoreAllMocks(); setUnauthorizedHandler(null); });

  it('posts idempotency key on mutations', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { revision: 3 }));
    await mrrc.cq(false);
    const [, init] = (globalThis.fetch as any).mock.calls[0];
    expect(init.method).toBe('POST');
    expect(init.headers['idempotency-key']).toBeTruthy();
  });

  it('tracks revision from responses', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { revision: 9 }));
    const { currentRevision } = await import('./mrrcClient');
    await mrrc.state();
    expect(currentRevision()).toBe(9);
  });

  it('fires the unauthorized handler on 401 outside /session/', async () => {
    vi.stubGlobal('fetch', mockFetch(401, { ok: false, reason: 'unauthenticated' }));
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    await mrrc.state();
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop/ft8web && npx vitest run src/services/mrrcClient.test.ts`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `mrrcClient.ts`**

`desktop/ft8web/src/services/mrrcClient.ts`:

```ts
// REST intent client for the MRRC-FT8 headless server (/api/v1).
// Mirrors the mobile PWA's api.js contract: JSON envelope with an `ok` flag,
// idempotency keys on mutations, revision tracking, and a 401 → login hook.

export interface DecodeCandidate {
  call: string;
  grid: string;
  snr: number;
  text: string;
  is_cq: boolean;
  slot_id: number;
  freq: number;
}

export interface ApiResult {
  ok: boolean;
  status: number;
  reason?: string;
  body: Record<string, any>;
}

let revision = 0;
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export function currentRevision(): number {
  return revision;
}

export function key(): string {
  return crypto.randomUUID();
}

async function request(
  path: string,
  options: { method?: string; body?: unknown; idempotencyKey?: string } = {},
): Promise<ApiResult> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers['content-type'] = 'application/json';
  if (options.idempotencyKey) headers['idempotency-key'] = options.idempotencyKey;
  const response = await fetch(`/api/v1${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    credentials: 'same-origin',
  });
  let payload: Record<string, any> = {};
  try { payload = await response.json(); } catch { /* empty body */ }
  if (typeof payload.revision === 'number') revision = payload.revision;
  if (response.status === 401 && !path.startsWith('/session/') && onUnauthorized) {
    onUnauthorized();
  }
  const ok = response.status >= 200 && response.status < 300 && payload.ok !== false;
  return { ok, status: response.status, reason: payload.reason, body: payload };
}

export const mrrc = {
  login: (password: string) => request('/session/login', { method: 'POST', body: { password } }),
  logout: () => request('/session/logout', { method: 'POST' }),
  currentSession: () => request('/session/current'),
  state: () => request('/state'),

  acquireLease: () => request('/lease/acquire', { method: 'POST', idempotencyKey: key() }),
  releaseLease: () => request('/lease/release', { method: 'POST', idempotencyKey: key() }),
  heartbeat: () => request('/lease/heartbeat', { method: 'POST' }),

  select: (c: DecodeCandidate) =>
    request('/operation/select', {
      method: 'POST', idempotencyKey: key(),
      body: {
        dx_call: c.call, dx_grid: c.grid || '', snr_db: c.snr,
        text: c.text, is_cq: c.is_cq, slot_id: c.slot_id, freq: c.freq,
      },
    }),
  reply: (c: DecodeCandidate) =>
    request('/operation/reply', {
      method: 'POST', idempotencyKey: key(),
      body: {
        dx_call: c.call, dx_grid: c.grid || '', snr_db: c.snr,
        text: c.text, is_cq: c.is_cq, slot_id: c.slot_id, freq: c.freq,
      },
    }),
  cq: (loop = false) =>
    request('/operation/cq', { method: 'POST', idempotencyKey: key(), body: { loop } }),
  txOff: () => request('/operation/enable_tx_off', { method: 'POST', idempotencyKey: key() }),
  stop: () => request('/operation/stop', { method: 'POST', idempotencyKey: key() }),
  clearFault: (interlock?: string) =>
    request('/operation/clear-fault', {
      method: 'POST', idempotencyKey: key(), body: interlock ? { interlock } : {},
    }),

  radioBand: (freqHz: number) =>
    request('/radio/band', { method: 'POST', idempotencyKey: key(), body: { freq_hz: freqHz } }),
  rigLevels: () => request('/radio/rig/levels'),
  rigLevel: (level: string, value: number) =>
    request('/radio/rig/level', { method: 'POST', idempotencyKey: key(), body: { level, value } }),
  rigMode: () => request('/radio/mode'),
  rigModeSet: (mode: string, passbandHz: number) =>
    request('/radio/mode', { method: 'POST', idempotencyKey: key(), body: { mode, passband_hz: passbandHz } }),
  rigFilter: (hz: number) =>
    request('/radio/filter', { method: 'POST', idempotencyKey: key(), body: { hz } }),

  qsos: () => request('/logs/qsos'),
  dxcc: () => request('/dxcc'),
  bandHunt: (params: Record<string, string>) => request(`/band-hunt?${new URLSearchParams(params)}`),
  settings: () => request('/settings'),
  putSetting: (k: string, v: unknown) =>
    request('/settings', { method: 'PUT', idempotencyKey: key(), body: { [k]: v } }),
};
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop/ft8web && npx vitest run src/services/mrrcClient.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/services/mrrcClient.ts desktop/ft8web/src/services/mrrcClient.test.ts
git commit -m "feat(desktop): mrrcClient REST adapter for the FT8 server"
```

---

### Task 3: `mrrcStreams.ts` — WebSocket streams + WF01 parser

**Files:**
- Create: `desktop/ft8web/src/services/mrrcStreams.ts`
- Test: `desktop/ft8web/src/services/mrrcStreams.test.ts`

**Interfaces:**
- Consumes: nothing (stands alone).
- Produces: `parseFrame(buffer: ArrayBuffer): WaterfallFrame | null` and `startStreams(cb: StreamCallbacks): StreamSet`. `WaterfallFrame = { seq, epochMs, binHz, bins: Uint8Array }`. `ServerDecodeMessage`/`ServerDecodeBatch` types matching the server's `/ws/v1/decodes` payload. `StreamSet` has `state`/`decodes`/`waterfall`, each `{ close(): void }`. Auth-failure (WS close code 4401) → `cb.onAuthFailure`.

- [ ] **Step 1: Write the failing tests**

`desktop/ft8web/src/services/mrrcStreams.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from 'vitest';
import { parseFrame, startStreams, type StreamCallbacks } from './mrrcStreams';

const HEADER = new Uint8Array([0x57, 0x46, 0x30, 0x31]); // "WF01"

function makeFrame(count: number): ArrayBuffer {
  const buf = new ArrayBuffer(22 + count);
  const view = new DataView(buf);
  new Uint8Array(buf, 0, 4).set(HEADER);
  view.setUint32(4, 7, true);              // seq
  view.setBigUint64(8, BigInt(1_755_000_000_000), true); // epoch ms
  view.setFloat32(16, 2.9296875, true);    // bin_hz
  view.setUint16(20, count, true);         // count
  new Uint8Array(buf, 22, count).fill(128);
  return buf;
}

describe('parseFrame', () => {
  it('parses a valid WF01 frame', () => {
    const frame = parseFrame(makeFrame(4));
    expect(frame).toEqual({ seq: 7, epochMs: 1755000000000, binHz: 2.9296875, bins: new Uint8Array([128, 128, 128, 128]) });
  });

  it('rejects a bad magic', () => {
    const buf = makeFrame(2);
    new Uint8Array(buf)[0] = 0x00;
    expect(parseFrame(buf)).toBeNull();
  });

  it('rejects a length mismatch', () => {
    expect(parseFrame(makeFrame(6))).toBeNull();
  });
});

describe('startStreams auth failure', () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it('calls onAuthFailure on close code 4401 and stops reconnecting', () => {
    let instance: any = null;
    class FakeWS {
      binaryType = '';
      onopen: (() => void) | null = null;
      onmessage: ((e: any) => void) | null = null;
      onclose: ((e: any) => void) | null = null;
      constructor(public url: string) { instance = this; }
      close() {}
    }
    vi.stubGlobal('WebSocket', FakeWS);
    const onAuthFailure = vi.fn();
    const cb: StreamCallbacks = {
      onState: () => {}, onDecodes: () => {}, onWaterfall: () => {},
      onAuthFailure,
    };
    startStreams(cb);
    instance.onclose?.({ code: 4401, reason: '' });
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd desktop/ft8web && npx vitest run src/services/mrrcStreams.test.ts`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `mrrcStreams.ts`**

`desktop/ft8web/src/services/mrrcStreams.ts`:

```ts
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd desktop/ft8web && npx vitest run src/services/mrrcStreams.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/services/mrrcStreams.ts desktop/ft8web/src/services/mrrcStreams.test.ts
git commit -m "feat(desktop): mrrcStreams WS adapter + WF01 waterfall parser"
```

---

### Task 4: `LoginView` component + auth gate

**Files:**
- Create: `desktop/ft8web/src/components/LoginView.tsx`
- Modify: `desktop/ft8web/src/App.tsx` (auth gate only — the rest of the brain swap is Tasks 5–12)

**Interfaces:**
- Consumes: `mrrc.login` (Task 2).
- Produces: `LoginView({ onLoggedIn })` — a centered dark login card. When the server restarts (session cookie invalidated), any 401 / WS 4401 routes back to login.

- [ ] **Step 1: Write the component**

`desktop/ft8web/src/components/LoginView.tsx`:

```tsx
import React, { useState } from 'react';
import { mrrc } from '../services/mrrcClient';

export function LoginView({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    const result = await mrrc.login(password);
    setBusy(false);
    if (result.ok) {
      onLoggedIn();
    } else if (result.reason === 'rate_limited') {
      setError('Too many attempts — wait a moment and retry');
    } else {
      setError('Login failed');
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-app text-text-main">
      <form onSubmit={submit} className="w-full max-w-xs rounded-xl border border-border-input bg-panel p-6 shadow-xl">
        <h1 className="mb-1 text-lg font-bold uppercase tracking-widest">MRRC-FT8</h1>
        <p className="mb-4 text-[10px] uppercase tracking-widest text-text-muted">Desktop Remote</p>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Station password"
          autoFocus
          className="mb-3 w-full rounded border border-border-input bg-app px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-[#4caf50] px-3 py-2 text-sm font-bold uppercase tracking-widest text-white disabled:opacity-50"
        >
          {busy ? 'Logging in…' : 'Log in'}
        </button>
        {error && <p className="mt-3 text-xs text-red-400">{error}</p>}
      </form>
    </div>
  );
}
```

- [ ] **Step 2: Wire the gate in App.tsx**

In `desktop/ft8web/src/App.tsx`, at the top of the `App` component, add state:

```tsx
const [loggedIn, setLoggedIn] = useState<boolean | null>(null);
```

Add an effect that checks the current session on mount (before any stream connects):

```tsx
useEffect(() => {
  mrrc.currentSession().then((res) => {
    setLoggedIn(res.ok);
  });
}, []);
```

At the component's return, wrap the whole app: when `loggedIn === false` render `<LoginView onLoggedIn={() => { setLoggedIn(true); }} />`; when `null` render a blank loading screen; otherwise render the existing tree.

- [ ] **Step 3: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: both pass. Manual: open the dev server, confirm the login card renders before auth.

- [ ] **Step 4: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/components/LoginView.tsx desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): login gate for the MRRC-FT8 server"
```

---

### Task 5: `useServerFT8` hook — connection, snapshot, lease

**Files:**
- Create: `desktop/ft8web/src/services/useServerFT8.ts`

**Interfaces:**
- Consumes: `mrrc` + `setUnauthorizedHandler` (Task 2), `startStreams`/`StreamSet` (Task 3).
- Produces — the hook used by all later App.tsx tasks:

```ts
interface ServerSnapshot {
  revision: number;
  lease: { held: boolean; mine: boolean };
  safety: { armed: boolean; ptt_on: boolean; faults: string[] };
  sequencer: { state: string; tx_enabled: boolean; dx_call: string };
  selected: { call: string; grid: string } | null;
  radio: { freq_hz: number | null };
  station: { my_call: string; my_grid: string; worked_calls: string[] };
  last_tx: { slot_id: number; utc: string; text: string; freq_hz: number } | null;
}

function useServerFT8(opts: { onLoggedOut: () => void }): {
  connected: boolean;
  snapshot: ServerSnapshot;
  lastDecodes: ServerDecodeBatch[];        // newest first, capped at 64
  waterfallRef: React.MutableRefObject<WaterfallFrame[]>; // drained by drawWaterfall
  ensureLease: () => Promise<boolean>;
  releaseLease: () => Promise<void>;
  connectedRef: React.MutableRefObject<boolean>;  // for the rAF loop
  snapshotRef: React.MutableRefObject<ServerSnapshot>; // latest snapshot for callbacks
}
```

`ensureLease()` acquires the control lease only if `lease.mine` is false and `lease.held` is false (mirrors the mobile PWA's implicit-lease, UC-002). A 15 s heartbeat runs while the lease is ours; it is released on unmount.

- [ ] **Step 1: Implement the hook**

`desktop/ft8web/src/services/useServerFT8.ts`:

```ts
import { useCallback, useEffect, useRef, useState } from 'react';
import { mrrc, setUnauthorizedHandler } from './mrrcClient';
import { startStreams, type ServerDecodeBatch, type StreamSet, type WaterfallFrame } from './mrrcStreams';

export interface ServerSnapshot {
  revision: number;
  lease: { held: boolean; mine: boolean };
  safety: { armed: boolean; ptt_on: boolean; faults: string[] };
  sequencer: { state: string; tx_enabled: boolean; dx_call: string };
  selected: { call: string; grid: string } | null;
  radio: { freq_hz: number | null };
  station: { my_call: string; my_grid: string; worked_calls: string[] };
  last_tx: { slot_id: number; utc: string; text: string; freq_hz: number } | null;
}

const EMPTY: ServerSnapshot = {
  revision: 0,
  lease: { held: false, mine: false },
  safety: { armed: false, ptt_on: false, faults: [] },
  sequencer: { state: 'idle', tx_enabled: false, dx_call: '' },
  selected: null,
  radio: { freq_hz: null },
  station: { my_call: '', my_grid: '', worked_calls: [] },
  last_tx: null,
};

export function useServerFT8(opts: { onLoggedOut: () => void }) {
  const [connected, setConnected] = useState(false);
  const [snapshot, setSnapshot] = useState<ServerSnapshot>(EMPTY);
  const [lastDecodes, setLastDecodes] = useState<ServerDecodeBatch[]>([]);
  const streamsRef = useRef<StreamSet | null>(null);
  const waterfallRef = useRef<WaterfallFrame[]>([]);
  const snapshotRef = useRef<ServerSnapshot>(EMPTY);
  const connectedRef = useRef(false);

  useEffect(() => {
    const onAuthFailure = () => {
      streamsRef.current?.state.close();
      opts.onLoggedOut();
    };
    setUnauthorizedHandler(onAuthFailure);

    const streams = startStreams({
      onState: (raw) => {
        const next = { ...snapshotRef.current, ...raw };
        snapshotRef.current = next;
        setSnapshot(next);
      },
      onDecodes: (batch) => setLastDecodes((prev) => [batch, ...prev].slice(0, 64)),
      onWaterfall: (frame) => {
        waterfallRef.current.push(frame);
        if (waterfallRef.current.length > 64) waterfallRef.current.shift();
      },
      onOpen: () => { connectedRef.current = true; setConnected(true); },
      onClose: () => { connectedRef.current = false; setConnected(false); },
      onAuthFailure,
    });
    streamsRef.current = streams;
    return () => {
      setUnauthorizedHandler(null);
      streams.state.close();
      streams.decodes.close();
      streams.waterfall.close();
    };
  }, [opts.onLoggedOut]);

  const ensureLease = useCallback(async (): Promise<boolean> => {
    if (snapshotRef.current.lease.mine) return true;
    if (snapshotRef.current.lease.held) return false;
    return (await mrrc.acquireLease()).ok;
  }, []);

  const releaseLease = useCallback(async (): Promise<void> => {
    await mrrc.releaseLease();
  }, []);

  // 15 s heartbeat while our session holds the control lease.
  useEffect(() => {
    const timer = setInterval(() => {
      if (snapshotRef.current.lease.mine) mrrc.heartbeat();
    }, 15_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => () => { void releaseLease(); }, [releaseLease]);

  return { connected, snapshot, lastDecodes, waterfallRef, ensureLease, releaseLease, connectedRef, snapshotRef };
}
```

- [ ] **Step 2: Verify it compiles**

Run: `cd desktop/ft8web && npm run lint`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/services/useServerFT8.ts
git commit -m "feat(desktop): useServerFT8 hook — streams, snapshot, implicit lease"
```

---

### Task 6: Strip the local DSP/audio/CAT/FSM brain from App.tsx

**Files:**
- Modify: `desktop/ft8web/src/App.tsx`

**Interfaces:**
- Consumes: `useServerFT8` (Task 5). Nothing that gets deleted here is used by later tasks.

This is the largest, riskiest task. Work in small commits, running `npm run lint` after each.

- [ ] **Step 1: Remove the imports**

Delete from the import block (top of `App.tsx`, currently ~lines 1–19): `getCaptureWorkletUrl` (from `./AudioWorkletBlob`), `encodeFT8, encodeFT4` (from `@e04/ft8ts`), `CatManager`, `UniversalSerialPort, WebSocketSerialPort`, `FT8FSM, { QueuedCaller }`, `logBook, QSO`, `CloudLogService`, `LogbookService`, `dxccService`, `externalStream`, `pskReporter, PSKReporterService`.

**Keep**: `extractTransmitterCallsign` from `./services/pskReporterSpot` (used for B4/N/W badge call extraction), the three components (`LogBookViewer`, `VersionInfo`, `WhatsNewModal`), `CHANGELOG`/`LATEST_UPDATE`.

Add:

```ts
import { useServerFT8 } from './services/useServerFT8';
import { mrrc } from './services/mrrcClient';
```

- [ ] **Step 2: Remove audio/DSP/CAT/FSM state and refs**

Delete every `useState`/`useRef` that exists solely for the local brain:
- Audio/DSP: `selectedDeviceId`, `selectedOutputDeviceId`, `audioLevel`, `decodeStats`, `serialPort`, `mediaStreamRef`, `sourceNodeRef`, `captureNodeRef`, `txSourceNodeRef`, `audioCtxRef`, `analyserRef`, `rxBufferRef`, `workerRef`, `pendingMarkersRef`, `catRef`.
- CAT: `catMode`, `catBaudRate`, `icomAddress`, `civWsUrl`, `cp2105Channel`, `catConnected`, `catTestResult`.
- Cloud/stream/psk: `wavelogEnabled`, `autoUploadCloudlog`, `wavelogUrl`, `wavelogApiKey`, `wavelogStationProfileId`, `streamEnabled`, `streamUrl`, `streamConnected`, `pskEnabled`, `pskSpotsSent`.
- Keep: `vfoFreq`, `txPeriod`, `mode` (locked to `'FT8'`), `audioActive` (reuse as the "connected" lamp), `utcTime`, `windowProgress`, `clockVerdict`, `myCall`/`myGrid` (read from server, see Task 9), `txFreq`, `rxLog`, `qsoLog`, `isTransmitting`, `isTxQueued`, `targetCall`, `txEnabled`, `autoSequence`, `fsmState`, `fsmQueue`, `theme`, `wakeLockEnabled` (delete if you prefer), `maxLogEntries`.

- [ ] **Step 3: Delete the audio/DSP effects and the worker**

Delete the effects that call `getUserMedia`, create the `AudioContext`/`AudioWorkletNode`/`AnalyserNode`, spawn `new Worker(...)`, poll `analyser`, and post to the worker. Delete `AudioWorkletBlob` usage entirely. Keep the rAF loop **only** for: the UTC clock (`utcTime`), the slot window progress bar, and `drawWaterfall` (Task 7 rewires its data source).

- [ ] **Step 4: Delete CAT wiring and `startTx`/`transmitMessage`**

Delete the CAT poll effect (the one calling `catRef.current.getFrequency()`), all `catRef.current.setTx(...)` calls, and the audio part of `startTx` (the `encodeFT8/encodeFT4` + `AudioBufferSourceNode` block). Task 8 replaces the TX trigger with server operations. Keep a `startTx(message)`-shaped function only if something renders its effect (otherwise delete).

- [ ] **Step 5: Delete the FSM instance and its callbacks**

Delete `fsmRef` construction and the `onStateChange`/`onTransmit`/`onAppendQsoLog`/`onLogQSO` wiring. Delete the `autoSequence` FSM drive logic (`onPeriodDecodeReady` calls). Task 8 reimplements CQ/Ans against the server.

- [ ] **Step 6: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: PASS. You may need several rounds to remove dead references (`unused` errors). Do not delete the `rxLog`/`qsoLog`/`drawWaterfall`/button JSX — those are rewired, not removed.

- [ ] **Step 7: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/App.tsx
git commit -m "refactor(desktop): strip local DSP/audio/CAT/FSM brain from App.tsx"
```

---

### Task 7: Wire server decodes into Band Activity + Active QSO

**Files:**
- Modify: `desktop/ft8web/src/App.tsx`

**Interfaces:**
- Consumes: `useServerFT8().lastDecodes` (Task 5), the server's `ServerDecodeMessage` shape (Task 3), existing `FT8DecodedMessage` render shape.
- Note: `App.tsx` needs `import type { ServerDecodeMessage } from '../services/mrrcStreams';`

- [ ] **Step 1: Map server batches into `FT8DecodedMessage[]`**

Add a pure helper at module scope:

```tsx
function serverMessageToRow(m: ServerDecodeMessage, slotId: number): FT8DecodedMessage {
  return {
    time: String(slotId * 15).padStart(6, '0'),  // HHMMSS slot start
    snr: m.snr,
    freq: Math.round(m.freq),
    message: m.text,
    isIncoming: m.to_me,
  };
}
```

(`time` here is the slot-start HHMMSS so the divider shows the UTC batch time; the Band Activity row template already formats it.)

- [ ] **Step 2: Feed `rxLog` from the server decode batches**

Replace the `worker.onmessage` handler's decode push with an effect on `lastDecodes` (cap at 50 rows — `maxLogEntries` is a settings state you may read via a ref if one already exists, otherwise hard-code 50):

```tsx
useEffect(() => {
  const rows: FT8DecodedMessage[] = [];
  for (const batch of lastDecodes) {
    const divider: FT8DecodedMessage = {
      time: String(batch.slot_id * 15).padStart(6, '0'),
      snr: 0, freq: 0, message: '', isDivider: true, periodIndex: batch.slot_id % 2,
    };
    rows.push(divider);
    for (const m of batch.messages) rows.push(serverMessageToRow(m, batch.slot_id));
  }
  setRxLog(rows.slice(0, 50));
}, [lastDecodes]);
```

Keep the existing slot-divider rendering (a `isDivider` row). Remove the old `worker.onmessage`/`rememberHashCalls` decode path.

- [ ] **Step 3: Build `qsoLog` (Active QSO) from server state + decodes**

Compute rows for the Active QSO panel from: (a) messages addressed to us (`to_me`) or from the selected station, (b) our own echoes (`mine`), and (c) `snapshot.last_tx` rendered as an outgoing (TX) row. Store these in the existing `qsoLog` state via an effect over `lastDecodes` + `snapshot`. TX rows keep `isTx: true` so the template colors them sky-blue.

- [ ] **Step 4: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: PASS. Manual: with the server running on a band with decodes, confirm rows appear under slot dividers and `<- `-colored rows reach Active QSO.

- [ ] **Step 5: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): feed Band Activity and Active QSO from server decode stream"
```

---

### Task 8: Rewire the waterfall to server WF01 frames

**Files:**
- Modify: `desktop/ft8web/src/App.tsx`

**Interfaces:**
- Consumes: `useServerFT8().waterfallRef` (Task 5), `parseFrame` output shape (Task 3).

- [ ] **Step 1: Replace the AnalyserNode source in `drawWaterfall`**

Inside `drawWaterfall`, replace the `analyser.getByteFrequencyData(dataArray)` read with a drain of `waterfallRef.current`:

```tsx
const frames = waterfallRef.current.splice(0, waterfallRef.current.length);
for (const frame of frames) {
  // frame.bins (0..~255, server maps 0–3000 Hz across them) becomes one
  // horizontal row. Reuse the existing colorFor/palette mapping — it already
  // consumes a Uint8Array of 0..255 values.
  shiftCanvasDownAndDrawRow(frame.bins);
}
```

Keep the existing row-scroll (`ctx.drawImage` shift + `createImageData`) and the TX-frequency overlay bar (driven by `txFreq`). Freeze during TX as before (via `isTransmittingRef`). When `waterfallRef` is empty, keep the previous frame on screen (do not blank).

- [ ] **Step 2: Remove the analyser/audio plumbing for the waterfall**

Delete the `AnalyserNode` creation and any audio-related `drawWaterfall` gating. The canvas resize + theme remain.

- [ ] **Step 3: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: PASS. Manual: with the server running, confirm the waterfall scrolls with live frames and shows the red TX bar at `txFreq`.

- [ ] **Step 4: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): render waterfall from server WF01 frames"
```

---

### Task 9: Rewire TX controls (CQ / Ans / TX-enable / STOP) to the server

**Files:**
- Modify: `desktop/ft8web/src/App.tsx`

**Interfaces:**
- Consumes: `mrrc.select/reply/cq/txOff/stop/clearFault` (Task 2), `ensureLease`/`snapshot` (Task 5).

Behavior contract (mirrors the mobile PWA + §10.3):
- **Click a Band Activity row** → `ensureLease()` (implicit), then `mrrc.select(row)`; set `targetCall`. Selecting never transmits.
- **CQ button** → `ensureLease()`, then `mrrc.cq(false)` (single CQ) — the server sequencer runs the whole CQ loop.
- **Ans button** → `ensureLease()`, then `mrrc.reply(row)`. The server returns `scheduled_tx`; if `deferred`, show "Reply armed → TX at HH:MM:SS UTC".
- **TX enable toggle** → when off, `mrrc.txOff()` (disarm). When on, CQ/Ans as above.
- **STOP TX** → `mrrc.stop()` — works without a lease (NFR-038). Always enabled.
- **Auto Sequence toggle** → maps to the server setting `auto_call_new_dxcc` via `mrrc.putSetting('auto_call_new_dxcc', on)`.

- [ ] **Step 1: Replace the TX trigger functions**

Replace `startTx`/`transmitMessage` bodies with the server calls above. The CQ/Ans button onClick handlers call the new async handlers; keep the JSX.

- [ ] **Step 2: Delete the FSM/TX-audio leftovers**

Remove `queuedTxMessageRef`, the `onTransmit`/`onAppendQsoLog` FSM callback wiring, and any remaining `startTx` audio code. `isTransmitting`/`isTxQueued` now derive from `snapshot.sequencer.state` (see Task 10) — set them from the snapshot effect, not from local TX.

- [ ] **Step 3: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: PASS. Manual: with the server running, click a decode → toast "Reply armed"; confirm the lease flips to ours in the UI.

- [ ] **Step 4: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): drive TX via server operations with implicit lease"
```

---

### Task 10: Wire server state → status UI (sequencer, lease, radio, TX)

**Files:**
- Modify: `desktop/ft8web/src/App.tsx`

**Interfaces:**
- Consumes: `useServerFT8().snapshot` (Task 5).

- [ ] **Step 1: Map snapshot into UI state**

Add an effect over `snapshot` that sets the existing state variables:

```tsx
useEffect(() => {
  setVfoFreq(snapshot.radio.freq_hz ?? vfoFreqRef.current);
  setMyCall(snapshot.station.my_call || myCallRef.current);
  setMyGrid(snapshot.station.my_grid || myGridRef.current);
  const inQso =
    snapshot.sequencer.tx_enabled &&
    snapshot.sequencer.state !== 'idle' &&
    snapshot.sequencer.state !== 'done';
  setIsTxQueued(inQso);                 // TX happens on eligible slots while a QSO is active
  setFsmState(mapSequencerState(snapshot.sequencer.state));
}, [snapshot]);
```

Map `sequencer.state` values (from `server/engine/sequencer.py` `QSOState`: `idle`, `calling`, `replying`, `report`, `roger_report`, `rogers`, `signoff`, `done`) to FT8web's FSM labels via a small lookup table:

```tsx
const SEQUENCER_STATE_LABELS: Record<string, string> = {
  idle: 'IDLE', calling: 'CQ_SENDING', replying: 'REPLY_SENDING',
  report: 'SENDING_REPORT', roger_report: 'SENDING_R_REPORT',
  rogers: 'SENDING_RR73', signoff: 'SENDING_73', done: 'IDLE',
};
function mapSequencerState(s: string): string {
  return SEQUENCER_STATE_LABELS[s] ?? s.toUpperCase();
}
```

Note: the server snapshot exposes `tx_enabled` and the QSO phase, not a per-slot "currently keying" flag. If the UI wants a "Transmitting…" pulse, drive it from `snapshot.last_tx` freshness (set when `last_tx.utc` matches the current slot, clear after ~12.6 s) instead of from a state that doesn't exist.

- [ ] **Step 2: Show lease + radio status**

Show an OBSERVER / CONTROLLER badge from `snapshot.lease.mine`; disable CQ/Ans when `snapshot.lease.held && !snapshot.lease.mine`. Use `connected` (from the hook) for the "Activate Audio"/server-connection lamp and the `audioLevel` VU bar area (replace it with a server `AUDIO` interlock lamp: green when no audio fault, red when `snapshot.safety.faults` includes the audio interlock).

- [ ] **Step 3: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: PASS. Manual: confirm frequency/call/grid populate from the server and the sequencer label tracks TX.

- [ ] **Step 4: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): surface server state — sequencer, lease, radio, safety"
```

---

### Task 11: Server-backed logbook

**Files:**
- Modify: `desktop/ft8web/src/components/LogBookViewer.tsx`
- Modify: `desktop/ft8web/src/App.tsx`

**Interfaces:**
- Consumes: `mrrc.qsos()` (Task 2). The server's `/logs/qsos` returns `{ ok, qsos: [...] }` with fields `id, my_call, dx_call, dx_grid, report_sent, report_rcvd, started_utc, mode, freq_hz, band, status, completed_epoch`.

- [ ] **Step 1: Replace the IndexedDB read path**

In `LogBookViewer.tsx`, replace `logBook.getAllQSOs()` with a fetch to `mrrc.qsos()` and map each server QSO to the viewer's row shape:

```tsx
const data = await mrrc.qsos();
const qsos = (data.body.qsos ?? []).map((q: any) => ({
  call: q.dx_call,
  qso_date: String(q.started_utc).slice(0, 8),
  time_on: String(q.started_utc).slice(8, 14),
  band: q.band,
  mode: q.mode,
  freq: (q.freq_hz ?? 0) / 1e6,
  gridsquare: q.dx_grid,
  rst_sent: q.report_sent ?? '',
  rst_rcvd: q.report_rcvd ?? '',
}));
```

Remove the delete/clear/Wavelog-sync actions (the server is authoritative; voiding a QSO is out of v1 scope — leave `Wipe Log`/row-delete disabled or removed). Keep the ADIF export link pointed at `/api/v1/logs/adif` (same origin, so a plain `<a href>` download works).

- [ ] **Step 2: Drop the Wavelog/Cloudlog props from the viewer**

Update the `<LogBookViewer ... />` usage in `App.tsx` to drop the cloud-log props; delete `CloudLogService` references (already gone in Task 6).

- [ ] **Step 3: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: PASS. Manual: confirm logged QSOs appear after a completed QSO.

- [ ] **Step 4: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/components/LogBookViewer.tsx desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): server-backed logbook viewer"
```

---

### Task 12: Server-backed DXCC badges + band bar (4 bands, FT8 only)

**Files:**
- Modify: `desktop/ft8web/src/App.tsx`

**Interfaces:**
- Consumes: server decode field `is_new_dxcc` (Task 3), `snapshot.station.worked_calls` (Task 5), `mrrc.dxcc()` (Task 2).

- [ ] **Step 1: DXCC N/W badges from the server**

The server already marks each decode with `is_new_dxcc` (global entity-not-yet-worked). Replace the local `workedDxccEntities`/`dxccService.lookup` N/W computation with:

- **N badge** when `m.is_new_dxcc` is true.
- **W badge** when false (entity already worked).
- **B4 badge** when `snapshot.station.worked_calls` contains the base callsign (`String(m.call).split('/')[0].toUpperCase()`), matching the mobile PWA's filter.

Remove the `dxccService.load()`/`loadWorkedDxccEntities()` effects. Keep the cyan DXCC-prefix badge only if you can cheaply get the prefix — the server snapshot does not carry entity prefixes per decode, so drop the prefix badge in v1 (keep N/W + B4).

- [ ] **Step 2: Band bar → the server's 4 bands; lock mode to FT8**

Replace `BAND_FREQS_FT8` with the server's band table (`server/engine/bands.py`):

```tsx
const BAND_FREQS = [
  { label: '40m', mhz: '7.0', hz: 7074000 },
  { label: '20m', mhz: '14.0', hz: 14074000 },
  { label: '15m', mhz: '21.0', hz: 21074000 },
  { label: '10m', mhz: '28.0', hz: 28074000 },
];
```

Band clicks call `mrrc.radioBand(hz)` (with `ensureLease()` implicit). Hide the FT4 mode toggle (fix `mode` to `'FT8'`). The VFO frequency readout stays read-only from `snapshot.radio.freq_hz` (Task 10); remove its inline-edit.

- [ ] **Step 3: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: PASS. Manual: confirm N/W/B4 badges render and band clicks tune the rig.

- [ ] **Step 4: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): server DXCC badges and 4-band FT8 band bar"
```

---

### Task 13: Server-backed settings modal

**Files:**
- Modify: `desktop/ft8web/src/App.tsx` (settings modal JSX + persistence)

**Interfaces:**
- Consumes: `mrrc.settings()/putSetting()` (Task 2).

- [ ] **Step 1: Replace the settings persistence source**

The server settings (`/api/v1/settings`) are: `decoder_profile` (0–4), `decoder_threads` (1–12), `waterfall_lines_per_second`, `cq_loop_idle_timeout_s`, `auto_call_new_dxcc`, `auto_band_hunt`. In the settings modal:

- Load on open: `await mrrc.settings()`.
- Save: `await mrrc.putSetting(key, value)` per changed key.
- Map FT8web's `decodeDepth` (1–3) → `decoder_profile` (1→0 fast, 2→3, 3→4) on save, and reverse on load.
- Keep local-only display prefs (theme, `maxLogEntries`) in localStorage as today.

- [ ] **Step 2: Remove the CAT / audio-device / Cloudlog / PSKReporter / external-stream / wake-lock sections**

Delete those settings sections and their state (most state already removed in Task 6). Keep: Station identity (read-only from server), Decoding (profile/threads), QSO behavior (auto-call-new-DXCC toggle, final message mode — leave as-is/local if unused by the server), Appearance (theme), Logbook (max entries). Add an explicit "Server" hint that radio/CAT is managed on the station.

- [ ] **Step 3: Verify**

Run: `cd desktop/ft8web && npm run lint && npm run build`
Expected: PASS. Manual: change `decoder_profile`, confirm it persists across reload.

- [ ] **Step 4: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/src/App.tsx
git commit -m "feat(desktop): server-backed settings, drop local CAT/cloud/psk sections"
```

---

### Task 14: Vite config — `/desktop/` base + dev proxy; final build + E2E

**Files:**
- Modify: `desktop/ft8web/vite.config.ts`
- Modify: `desktop/ft8web/package.json` (drop `@e04/ft8ts` if nothing imports it anymore)

**Interfaces:**
- Consumes: everything above.
- Produces: a build whose asset URLs are relative to `/desktop/`, a dev server that proxies `/api` and `/ws` to the loopback MRRC-FT8 server, and a verified end-to-end run against the real server.

- [ ] **Step 1: Base path + dev proxy**

In `desktop/ft8web/vite.config.ts`, set `base: '/desktop/'` and add a dev proxy:

```ts
export default defineConfig({
  base: '/desktop/',
  // ...existing plugins/define...
  server: {
    port: 3000,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
});
```

- [ ] **Step 2: Drop the unused DSP dependency**

In `desktop/ft8web/package.json`, remove `"@e04/ft8ts": ...` from `dependencies` (nothing imports it after Task 6). Run `npm install`.

- [ ] **Step 3: Strip the upstream analytics script**

`desktop/ft8web/index.html` embeds a third-party tracker (`https://stats.ok1cdj.com/analytics-x7f2`) inherited from upstream — remove that `<script>` tag and any associated script file references so the built `dist/index.html` ships no third-party analytics (deferred minor from Task 1).

- [ ] **Step 4: Full test + build**

```bash
cd desktop/ft8web
npm test            # vitest: mrrcClient + mrrcStreams suites
npm run lint
npm run build
```

Expected: all green; `dist/` written.

- [ ] **Step 4: End-to-end against the real server**

Start the loopback server (from repo root, in another shell):

```bash
OMP_STACKSIZE=10M venv/bin/python -m server.main
```

Then build the client dist and hit `/desktop`:

```bash
cd desktop/ft8web && npm run build
curl -s http://127.0.0.1:8000/desktop/ | head -5   # should serve the built index.html
```

Manual smoke test in a browser at `http://127.0.0.1:8000/desktop/`:
1. Login → cockpit appears.
2. Band Activity populates from the server decode stream (band must have signals).
3. Waterfall scrolls with live spectrum.
4. Click a decode → toast "Reply armed" (or lease rejection if another session holds control).
5. STOP TX is always enabled.

- [ ] **Step 5: Commit**

```bash
cd /Users/cheenle/HAM/ft8
git add desktop/ft8web/vite.config.ts desktop/ft8web/package.json desktop/ft8web/package-lock.json desktop/ft8web/dist/index.html
git commit -m "feat(desktop): /desktop base path, dev proxy, drop @e04/ft8ts; final build"
```
