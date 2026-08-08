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
    const buf = makeFrame(6);
    new DataView(buf).setUint16(20, 5, true); // header count (5) != payload bytes (6)
    expect(parseFrame(buf)).toBeNull();
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
    vi.stubGlobal('location', { protocol: 'http:', host: 'localhost' });
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
