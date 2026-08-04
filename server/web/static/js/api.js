// REST intent client: idempotency keys and revision tracking (§10.1).

let revision = 0;

export function currentRevision() {
  return revision;
}

async function request(path, { method = "GET", body, idempotencyKey } = {}) {
  const headers = {};
  if (body !== undefined) headers["content-type"] = "application/json";
  if (idempotencyKey) headers["idempotency-key"] = idempotencyKey;
  const response = await fetch(`/api/v1${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  let payload = {};
  try { payload = await response.json(); } catch { /* empty body */ }
  if (typeof payload.revision === "number") revision = payload.revision;
  // Sessions are in-memory (NFR-075): a server restart invalidates every
  // cookie. Reload so boot() lands on the login view instead of leaving a
  // dead cockpit. Session endpoints are excluded — boot/login handle them.
  if (response.status === 401 && !path.startsWith("/session/")) location.reload();
  return { status: response.status, ...payload };
}

export function key() {
  return crypto.randomUUID();
}

export const api = {
  login: (password) => request("/session/login", { method: "POST", body: { password } }),
  logout: () => request("/session/logout", { method: "POST" }),
  currentSession: () => request("/session/current"),
  state: () => request("/state"),

  acquireLease: () => request("/lease/acquire", { method: "POST", idempotencyKey: key() }),
  releaseLease: () => request("/lease/release", { method: "POST", idempotencyKey: key() }),
  heartbeat: () => request("/lease/heartbeat", { method: "POST" }),

  select: (candidate) =>
    request("/operation/select", {
      method: "POST",
      idempotencyKey: key(),
      body: {
        dx_call: candidate.call,
        dx_grid: candidate.grid || "",
        snr_db: candidate.snr,
        text: candidate.text,
        is_cq: candidate.is_cq,
        slot_id: candidate.slot_id,
      },
    }),
  reply: () => request("/operation/reply", { method: "POST", idempotencyKey: key() }),
  cq: (loop = false) =>
    request("/operation/cq", { method: "POST", idempotencyKey: key(), body: { loop } }),
  band: (freqHz) =>
    request("/radio/band", {
      method: "POST",
      idempotencyKey: key(),
      body: { freq_hz: freqHz },
    }),
  rigLevels: () => request("/radio/rig/levels"),
  rigLevel: (level, value) =>
    request("/radio/rig/level", {
      method: "POST",
      idempotencyKey: key(),
      body: { level, value },
    }),
  rigMode: () => request("/radio/mode"),
  rigModeSet: (mode, passbandHz) =>
    request("/radio/mode", {
      method: "POST",
      idempotencyKey: key(),
      body: { mode, passband_hz: passbandHz },
    }),
  rigFilter: (hz) =>
    request("/radio/filter", {
      method: "POST",
      idempotencyKey: key(),
      body: { hz },
    }),
  qsos: () => request("/logs/qsos"),
  dxcc: () => request("/dxcc"),
  bandHunt: (params) => request(`/band-hunt?${new URLSearchParams(params)}`),
  settings: () => request("/settings"),
  putSetting: (key, value) =>
    request("/settings", { method: "PUT", idempotencyKey: key(), body: { [key]: value } }),
  txOff: () => request("/operation/enable_tx_off", { method: "POST", idempotencyKey: key() }),
  stop: () => request("/operation/stop", { method: "POST", idempotencyKey: key() }),
  clearFault: (interlock) =>
    request("/operation/clear-fault", {
      method: "POST",
      idempotencyKey: key(),
      body: interlock ? { interlock } : {},
    }),
};
