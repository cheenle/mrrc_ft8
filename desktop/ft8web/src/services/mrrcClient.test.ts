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
