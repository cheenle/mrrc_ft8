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

  qsos: (sinceDays?: number) =>
    request(`/logs/qsos${sinceDays ? `?since_days=${sinceDays}` : ''}`),
  dxcc: () => request('/dxcc'),
  bandHunt: (params: Record<string, string>) => request(`/band-hunt?${new URLSearchParams(params)}`),
  settings: () => request('/settings'),
  putSetting: (k: string, v: unknown) =>
    request('/settings', { method: 'PUT', idempotencyKey: key(), body: { [k]: v } }),
};
