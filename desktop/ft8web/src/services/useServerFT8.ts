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

export function useServerFT8(opts: { onLoggedOut: () => void; enabled?: boolean }) {
  const { onLoggedOut, enabled = true } = opts;
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
      onLoggedOut();
    };
    setUnauthorizedHandler(onAuthFailure);

    if (!enabled) {
      // Not logged in: keep the streams closed and the connected flag false.
      // The gate flips this effect's dependencies when `enabled` changes, so
      // the streams start on login and close on logout / auth failure.
      streamsRef.current?.state.close();
      streamsRef.current?.decodes.close();
      streamsRef.current?.waterfall.close();
      streamsRef.current = null;
      connectedRef.current = false;
      setConnected(false);
      return () => setUnauthorizedHandler(null);
    }

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
      if (streamsRef.current === streams) streamsRef.current = null;
      connectedRef.current = false;
      setConnected(false);
    };
  }, [onLoggedOut, enabled]);

  const ensureLease = useCallback(async (): Promise<boolean> => {
    if (snapshotRef.current.lease.mine) return true;
    if (snapshotRef.current.lease.held) return false;
    return (await mrrc.acquireLease()).ok;
  }, []);

  const releaseLease = useCallback(async (): Promise<void> => {
    await mrrc.releaseLease();
  }, []);

  // Empty the decode + waterfall buffers (e.g. after a band switch) so stale
  // rows from the previous band don't linger and reappear from the replay
  // buffer.  The stream effect keeps filling both refs from the server.
  const clearDecodes = useCallback((): void => {
    setLastDecodes([]);
    waterfallRef.current = [];
  }, []);

  // 15 s heartbeat while our session holds the control lease.
  useEffect(() => {
    const timer = setInterval(() => {
      if (snapshotRef.current.lease.mine) mrrc.heartbeat();
    }, 15_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => () => { void releaseLease(); }, [releaseLease]);

  return { connected, snapshot, lastDecodes, waterfallRef, ensureLease, releaseLease, clearDecodes, connectedRef, snapshotRef };
}
