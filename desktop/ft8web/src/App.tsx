import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Activity, Settings, X, HelpCircle, Square } from 'lucide-react';

import { LogBookViewer } from './components/LogBookViewer';
import { LoginView } from './components/LoginView';
import { VersionInfo } from './components/VersionInfo';
import { WhatsNewModal } from './components/WhatsNewModal';
import { CHANGELOG, LATEST_UPDATE, type ChangelogEntry } from './changelog';
import { extractTransmitterCallsign } from './services/pskReporterSpot';
import { mrrc, type DecodeCandidate } from './services/mrrcClient';
import { useServerFT8 } from './services/useServerFT8';
import type { ServerDecodeMessage } from './services/mrrcStreams';

export interface FT8DecodedMessage {
  time: string;
  snr: number;
  freq: number;
  message: string;
  periodIndex?: number;
  isDivider?: boolean;
  isTx?: boolean;
  isIncoming?: boolean;
  // Server decode fields threaded through for TX intent (Task 9): /operation/
  // select|reply take the raw DX call/grid/flag and the UTC slot id.
  call?: string;
  grid?: string;
  isCq?: boolean;
  slotId?: number;
}

// --- Server decode → row mapping (Task 7) ------------------------------------
// The server decodes once per UTC 15-second slot and reports batches keyed by
// `slot_id` = floor(epoch/15). slot_id*15 is therefore the slot-start epoch in
// seconds; modulo 86400 gives seconds since UTC midnight, which we format as the
// HHMMSS the Band Activity row template reads (time + period-parity fallback).
function slotTimeToHHMMSS(slotId: number, periodSec = 15): string {
  const sec = (slotId * periodSec) % 86400;
  const hh = String(Math.floor(sec / 3600)).padStart(2, '0');
  const mm = String(Math.floor((sec % 3600) / 60)).padStart(2, '0');
  const ss = String(sec % 60).padStart(2, '0');
  return `${hh}${mm}${ss}`;
}

function serverMessageToRow(m: ServerDecodeMessage, slotId: number): FT8DecodedMessage {
  return {
    time: slotTimeToHHMMSS(slotId), // HHMMSS slot start
    snr: m.snr,
    freq: Math.round(m.freq),
    message: m.text,
    isIncoming: m.to_me,
    call: m.call,
    grid: m.grid || '',
    isCq: m.is_cq,
    slotId,
  };
}

// Row → server candidate (Task 9): /operation/select|reply take the raw
// DecodeCandidate shape. The Active QSO window prepends "<- " to incoming
// messages, so the candidate text is the prefix-stripped decode.
function rowToCandidate(row: FT8DecodedMessage): DecodeCandidate | null {
  const call = (row.call ?? extractTransmitterCallsign(row.message) ?? '').toUpperCase();
  if (!call) return null;
  return {
    call,
    grid: (row.grid ?? '').toUpperCase(),
    snr: row.snr,
    text: row.message.replace(/^<-?\s+/, '').replace(/^->\s+/, '').trim(),
    is_cq: row.isCq ?? false,
    slot_id: row.slotId ?? 0,
    freq: row.freq,
  };
}

// --- Sequencer phase → FT8web FSM label (Task 10) --------------------------
// The server snapshot exposes sequencer.state as one of the QSOState values
// (server/engine/sequencer.py: idle, calling, replying, report, roger_report,
// rogers, signoff, done). Map each phase onto the FSM labels the footer already
// renders; unknown values fall back to an uppercased copy of the raw state.
const SEQUENCER_STATE_LABELS: Record<string, string> = {
  idle: 'IDLE', calling: 'CQ_SENDING', replying: 'REPLY_SENDING',
  report: 'SENDING_REPORT', roger_report: 'SENDING_R_REPORT',
  rogers: 'SENDING_RR73', signoff: 'SENDING_73', done: 'IDLE',
};
function mapSequencerState(s: string): string {
  return SEQUENCER_STATE_LABELS[s] ?? s.toUpperCase();
}

// --- Advisory clock-accuracy check (SNTP-style over HTTP) -------------------
// FT8 is time-critical. Browsers can't read the system NTP daemon or set the
// clock, so we measure the device-clock offset against a trusted HTTP time
// source and only DISPLAY a status. The fix for bad drift is to correct the
// device clock, which is what every timing path in this app already relies on.
export type ClockStatus = 'ok' | 'warn' | 'bad' | 'unknown';

export interface ClockVerdict {
  status: ClockStatus;
  offsetMs: number;
  message: string;
}

// Time source: the app's OWN origin. We read the server's `Date` response
// header — no CORS, no third-party dependency, and the service worker passes
// same-origin requests fine. Resolution is 1 second, which is exactly right for
// an advisory "is the clock badly wrong?" check.
async function sampleClockOffset(): Promise<{ offsetMs: number; uncertaintyMs: number }> {
  const tx = Date.now();
  // Cache-buster + no-store → forces a fresh network response whose `Date`
  // header is the server's current UTC time (not a cached value).
  const res = await fetch(`/?_t=${tx}`, { method: 'GET', cache: 'no-store' });
  const rx = Date.now();
  const dateHeader = res.headers.get('date');
  if (!dateHeader) throw new Error('no Date header');
  const serverMs = Date.parse(dateHeader);           // truncated to whole second
  if (!Number.isFinite(serverMs)) throw new Error('bad Date header');
  const rtt = rx - tx;
  // +500ms: the header floors to the second, so the true instant is ~mid-second.
  return { offsetMs: serverMs + 500 - (tx + rtt / 2), uncertaintyMs: rtt / 2 + 500 };
}

async function checkClock(): Promise<ClockVerdict> {
  let best: { offsetMs: number; uncertaintyMs: number } | null = null;
  try {
    for (let i = 0; i < 5; i++) {
      const s = await sampleClockOffset();             // keep the lowest-jitter sample
      if (!best || s.uncertaintyMs < best.uncertaintyMs) best = s;
    }
  } catch {
    return { status: 'unknown', offsetMs: 0, message: 'Time check unavailable' };
  }
  if (!best) return { status: 'unknown', offsetMs: 0, message: 'Time check unavailable' };

  const drift = Math.abs(best.offsetMs);
  const sign = best.offsetMs >= 0 ? '+' : '\u2212';
  const offsetStr = `${sign}${(drift / 1000).toFixed(2)}s`;

  // Thresholds match a ~1s-resolution source and FT8 tolerance: <1s fine,
  // 1-2s decoding degrades, >2s won't decode. Guard avoids false warnings.
  if (drift <= Math.max(best.uncertaintyMs, 50) || drift < 1000) {
    return { status: 'ok', offsetMs: best.offsetMs, message: `Clock OK (${offsetStr})` };
  }
  if (drift < 2000) {
    return { status: 'warn', offsetMs: best.offsetMs, message: `Off ${offsetStr} \u2014 watch it` };
  }
  return { status: 'bad', offsetMs: best.offsetMs, message: `Off ${offsetStr} \u2014 fix device clock` };
}

export default function App() {
  const [loggedIn, setLoggedIn] = useState<boolean | null>(null);

  useEffect(() => {
    mrrc.currentSession().then((res) => {
      setLoggedIn(res.ok);
    });
  }, []);

  const BAND_FREQS_FT8 = [
    { label: '80m', mhz: '3.5', hz: 3573000 },
    { label: '40m', mhz: '7.0', hz: 7074000 },
    { label: '30m', mhz: '10.1', hz: 10136000 },
    { label: '20m', mhz: '14.0', hz: 14074000 },
    { label: '17m', mhz: '18.1', hz: 18100000 },
    { label: '15m', mhz: '21.0', hz: 21074000 },
    { label: '12m', mhz: '24.9', hz: 24915000 },
    { label: '10m', mhz: '28.0', hz: 28074000 },
    { label: '6m', mhz: '50.3', hz: 50313000 },
    { label: '2m', mhz: '144.1', hz: 144174000 },
    { label: '70cm', mhz: '432.1', hz: 432174000 },
    { label: '23cm', mhz: '1296.1', hz: 1296174000 }
  ];

  const BAND_FREQS_FT4 = [
    { label: '80m', mhz: '3.5', hz: 3575000 },
    { label: '40m', mhz: '7.0', hz: 7047500 },
    { label: '30m', mhz: '10.1', hz: 10140000 },
    { label: '20m', mhz: '14.0', hz: 14080000 },
    { label: '17m', mhz: '18.1', hz: 18104000 },
    { label: '15m', mhz: '21.1', hz: 21140000 },
    { label: '12m', mhz: '24.9', hz: 24919000 },
    { label: '10m', mhz: '28.1', hz: 28180000 },
    { label: '6m',  mhz: '50.3', hz: 50318000 },
    { label: '2m',  mhz: '144.1', hz: 144170000 }
  ];

  const [vfoFreq, setVfoFreq] = useState<number>(() => {
    const saved = localStorage.getItem('ft8_vfoFreq');
    return saved ? Number(saved) : 14074000;
  });
  const [editingVfo, setEditingVfo] = useState(false);
  const [vfoInputStr, setVfoInputStr] = useState('');

  useEffect(() => {
    localStorage.setItem('ft8_vfoFreq', vfoFreq.toString());
  }, [vfoFreq]);

  const [txPeriod, setTxPeriod] = useState<number>(() => {
    const saved = localStorage.getItem('ft8_txPeriod');
    return saved !== null ? Number(saved) : 0; // 0 = Even, 1 = Odd
  });

  useEffect(() => {
    localStorage.setItem('ft8_txPeriod', txPeriod.toString());
  }, [txPeriod]);

  const [mode, setMode] = useState<'FT8' | 'FT4'>(() =>
    (localStorage.getItem('ft8_mode') as 'FT8' | 'FT4') || 'FT8'
  );
  useEffect(() => { localStorage.setItem('ft8_mode', mode); }, [mode]);

  const BAND_FREQS = mode === 'FT4' ? BAND_FREQS_FT4 : BAND_FREQS_FT8;

  // Global Audio State
  const [audioActive, setAudioActive] = useState(false);
  const [utcTime, setUtcTime] = useState('00:00:00');
  const [windowProgress, setWindowProgress] = useState(0);
  const [clockVerdict, setClockVerdict] = useState<ClockVerdict>({ status: 'unknown', offsetMs: 0, message: 'Checking\u2026' });

  // Server connection (Task 5 hook). audioActive mirrors the server connection
  // and doubles as the "connected" lamp (Task 10 replaces the VU/interlock area).
  const handleLoggedOut = useCallback(() => { setLoggedIn(false); }, []);
  // Gate the hook's streams on login state so they don't open before the session
  // is validated and reconnect after a fresh login (Task 6 review fix).
  const { connected, lastDecodes, snapshot, waterfallRef, ensureLease } = useServerFT8({ onLoggedOut: handleLoggedOut, enabled: loggedIn === true });
  useEffect(() => { setAudioActive(connected); }, [connected]);

  // Advisory clock-accuracy check: measures device-clock drift vs a trusted
  // network source and updates the status light. Never sets the system clock.
  useEffect(() => {
    let alive = true;
    const run = () => { checkClock().then(v => { if (alive) setClockVerdict(v); }); };

    run();                                            // on mount
    const hourly = setInterval(run, 60 * 60 * 1000);  // hourly backstop

    const onVisible = () => { if (document.visibilityState === 'visible') run(); };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('online', run);           // network restored

    // Wake-from-sleep watchdog: a >35s gap between 30s ticks means the device
    // suspended; uses only the delta, so it works even if the clock is wrong.
    let last = Date.now();
    const wake = setInterval(() => {
      const now = Date.now();
      if (now - last > 35000) run();
      last = now;
    }, 30000);

    return () => {
      alive = false;
      clearInterval(hourly);
      clearInterval(wake);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('online', run);
    };
  }, []);
  
  // Station Configuration State
  const [myCall, setMyCall] = useState<string>(() => localStorage.getItem('ft8_myCall') || 'N0TMP');
  const [myGrid, setMyGrid] = useState<string>(() => localStorage.getItem('ft8_myGrid') || 'EM12');
  const [txFreq, setTxFreq] = useState<number>(() => {
      const saved = localStorage.getItem('ft8_txFreq');
      return saved ? Number(saved) : 1500;
  }); // Default TX offset
  const [decodeDepth, setDecodeDepth] = useState<number>(() => {
      const saved = localStorage.getItem('ft8_decodeDepth');
      return saved ? Number(saved) : 2;
  });
  const [maxRetries, setMaxRetries] = useState<number>(() => {
      const saved = localStorage.getItem('ft8_maxRetries');
      return saved ? Number(saved) : 4;
  });
  const [finalMessageMode, setFinalMessageMode] = useState<'RR73'|'RRR'>(() => {
      return (localStorage.getItem('ft8_finalMessageMode') as 'RR73'|'RRR') || 'RR73';
  });
  const [skipTx1Grid, setSkipTx1Grid] = useState<boolean>(() => {
      return localStorage.getItem('ft8_skipTx1Grid') === 'true';
  });

  const [dxccIgnoreMode, setDxccIgnoreMode] = useState<boolean>(() => {
    return localStorage.getItem('ft8_dxccIgnoreMode') === 'true';
  });

  // Helper to determine band from VFO frequency
  const getBandFromFreq = useCallback((freqInHz: number): string => {
      const mhz = freqInHz / 1e6;
      if (mhz >= 1.8 && mhz <= 2.0) return "160m";
      if (mhz >= 3.5 && mhz <= 4.0) return "80m";
      if (mhz >= 5.3 && mhz <= 5.4) return "60m";
      if (mhz >= 7.0 && mhz <= 7.3) return "40m";
      if (mhz >= 10.1 && mhz <= 10.2) return "30m";
      if (mhz >= 14.0 && mhz <= 14.35) return "20m";
      if (mhz >= 18.068 && mhz <= 18.168) return "17m";
      if (mhz >= 21.0 && mhz <= 21.45) return "15m";
      if (mhz >= 24.89 && mhz <= 24.99) return "12m";
      if (mhz >= 28.0 && mhz <= 29.7) return "10m";
      if (mhz >= 50.0 && mhz <= 54.0) return "6m";
      return "";
  }, []);


  const [maxLogEntries, setMaxLogEntries] = useState<number>(() => {
    const saved = localStorage.getItem('ft8_maxLogEntries');
    return saved ? Number(saved) : 50;
  });


  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    return (localStorage.getItem('ft8_theme') as 'dark' | 'light') || 'dark';
  });

  useEffect(() => {
    localStorage.setItem('ft8_theme', theme);
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  useEffect(() => {
      localStorage.setItem('ft8_myCall', myCall);
      localStorage.setItem('ft8_myGrid', myGrid);
      localStorage.setItem('ft8_txFreq', txFreq.toString());
      localStorage.setItem('ft8_decodeDepth', decodeDepth.toString());
      localStorage.setItem('ft8_maxRetries', maxRetries.toString());
      localStorage.setItem('ft8_finalMessageMode', finalMessageMode);
      localStorage.setItem('ft8_maxLogEntries', maxLogEntries.toString());
      localStorage.setItem('ft8_skipTx1Grid', String(skipTx1Grid));
      localStorage.setItem('ft8_dxccIgnoreMode', String(dxccIgnoreMode));
  }, [myCall, myGrid, txFreq, decodeDepth, maxRetries, finalMessageMode, maxLogEntries, skipTx1Grid, dxccIgnoreMode]);

  // UI State
  const [showSettings, setShowSettings] = useState(false);
  const [showAbout, setShowAbout] = useState(false);
  const [whatsNewEntries, setWhatsNewEntries] = useState<ChangelogEntry[]>([]);

  // Captured during the first render — BEFORE the settings-persistence effects run
  // — so we can tell a returning user (already has ft8_* settings) from a brand-new
  // one. This is what makes the dialog appear for existing users on the very first
  // build that ships it (they have no ft8_lastSeenUpdate key yet).
  const isReturningUserRef = useRef(
    typeof localStorage !== 'undefined' &&
    Object.keys(localStorage).some(k => k.startsWith('ft8_') && k !== 'ft8_lastSeenUpdate')
  );

  // Show the "What's New" dialog once when a returning user loads a newer build.
  useEffect(() => {
    if (!LATEST_UPDATE) return;
    const lastSeen = localStorage.getItem('ft8_lastSeenUpdate');
    if (lastSeen === LATEST_UPDATE) return;

    if (lastSeen !== null) {
      // Returning user: show every entry newer than the one they last saw. If the
      // stored date is unknown (older than the changelog), show just the latest.
      const idx = CHANGELOG.findIndex(e => e.date === lastSeen);
      setWhatsNewEntries(idx > 0 ? CHANGELOG.slice(0, idx) : [CHANGELOG[0]]);
      return;
    }

    // No stored marker yet.
    if (isReturningUserRef.current) {
      // Existing user meeting this feature for the first time -> show latest notes.
      setWhatsNewEntries([CHANGELOG[0]]);
    } else {
      // Brand-new user: nothing to catch up on — record the current build silently.
      localStorage.setItem('ft8_lastSeenUpdate', LATEST_UPDATE);
    }
  }, []);

  const closeWhatsNew = useCallback(() => {
    localStorage.setItem('ft8_lastSeenUpdate', LATEST_UPDATE);
    setWhatsNewEntries([]);
  }, []);
  const vfoFreqRef = useRef(14074000);

  useEffect(() => {
    vfoFreqRef.current = vfoFreq;
  }, [vfoFreq]);

  const selectBand = (hz: number) => {
    setVfoFreq(hz);
    setRxLog([]);
    setQsoLog([]);
  };

  const commitVfoInput = () => {
    setEditingVfo(false);
    const mhz = parseFloat(vfoInputStr.replace(',', '.'));
    if (!isNaN(mhz) && mhz >= 1 && mhz <= 450) {
      selectBand(Math.round(mhz * 1_000_000));
    }
  };

  const formatFrequency = (hz: number) => {
    return hz.toLocaleString('en-US').replace(/,/g, '.') + ' Hz';
  };

  // App State
  const [rxLog, setRxLog] = useState<FT8DecodedMessage[]>([]);
  const [qsoLog, setQsoLog] = useState<FT8DecodedMessage[]>([]);
  const [isTransmitting, setIsTransmitting] = useState(false);
  const isTransmittingRef = useRef(false);
  const [isTxQueued, setIsTxQueued] = useState(false);

  // Band Activity (rxLog) fed from the server decode stream (Task 7). Each UTC
  // slot batch gets a divider row; the list keeps the last 4 periods (up to 4
  // dividers, matching the pre-brainswap behavior) and is hard-capped at 50 rows.
  useEffect(() => {
    const rows: FT8DecodedMessage[] = [];
    let dividerCount = 0;
    for (const batch of lastDecodes) {
      if (dividerCount >= 4) break;
      const time = slotTimeToHHMMSS(batch.slot_id);
      rows.push({
        time,
        snr: 0,
        freq: 0,
        message: `-------- ${time.slice(0, 2)}:${time.slice(2, 4)}:${time.slice(4, 6)} UTC --------`,
        isDivider: true,
        periodIndex: batch.slot_id % 2,
      });
      dividerCount += 1;
      for (const m of batch.messages) rows.push(serverMessageToRow(m, batch.slot_id));
    }
    setRxLog(rows.slice(0, 50));
  }, [lastDecodes]);

  // Active QSO (qsoLog) from server decodes + snapshot (Task 7): messages
  // addressed to us (to_me) or from the selected station are incoming ("<- ",
  // green); our own echoes (mine) and the last transmitted message are outgoing
  // (isTx, sky-blue). Rebuilt newest-first, capped at 100 rows.
  useEffect(() => {
    const rows: FT8DecodedMessage[] = [];
    if (snapshot.last_tx) {
      rows.push({
        time: snapshot.last_tx.utc,
        snr: 0,
        freq: Math.round(snapshot.last_tx.freq_hz),
        message: snapshot.last_tx.text,
        isTx: true,
        slotId: snapshot.last_tx.slot_id,
      });
    }
    const selectedCall = (snapshot.selected?.call || '').toUpperCase();
    for (const batch of lastDecodes) {
      const time = slotTimeToHHMMSS(batch.slot_id);
      for (const m of batch.messages) {
        const fromSelected = selectedCall !== '' && m.call.toUpperCase() === selectedCall;
        const incoming = m.to_me || fromSelected;
        if (incoming || m.mine) {
          rows.push({
            time,
            snr: m.snr,
            freq: Math.round(m.freq),
            message: incoming ? `<- ${m.text}` : m.text,
            periodIndex: batch.slot_id % 2,
            isIncoming: incoming,
            isTx: m.mine,
            call: m.call,
            grid: m.grid || '',
            isCq: m.is_cq,
            slotId: batch.slot_id,
          });
        }
      }
    }
    setQsoLog(rows.slice(0, 100));
  }, [lastDecodes, snapshot]);
  
  // TX Controls State
  const [targetCall, setTargetCall] = useState('');
  const [txEnabled, setTxEnabled] = useState(false);
  // Last decode row selected for TX intent (Task 9): the Ans button replies to
  // this candidate. Cleared when the operator hand-edits the Target Station.
  const [selectedCandidate, setSelectedCandidate] = useState<DecodeCandidate | null>(null);
  // Minimal inline notice (no toast component in this file): "Reply armed →
  // TX at HH:MM:SS UTC" for deferred replies and lease-blocked controls.
  const [txNotice, setTxNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!txNotice) return;
    const t = setTimeout(() => setTxNotice(null), 5000);
    return () => clearTimeout(t);
  }, [txNotice]);

  // FSM State Machine Integration
  const [autoSequence, setAutoSequence] = useState<boolean>(() => {
    const saved = localStorage.getItem('ft8_autoSequence');
    return saved !== null ? saved === 'true' : true;
  });
  const [fsmState, setFsmState] = useState<string>('IDLE');
  // fsmRef was removed with the local FSM (Task 6). fsmQueue stays for the footer
  // render below and remains empty until Task 9/10 rewires it to the server.
  const [fsmQueue, setFsmQueue] = useState<{ callsign: string; report?: string | null; distance?: number }[]>([]);

  useEffect(() => {
    localStorage.setItem('ft8_autoSequence', String(autoSequence));
  }, [autoSequence]);

  // State mirrors retained for later tasks (Task 10 reads myCall/myGrid via refs).
  const myCallRef = useRef<string>(myCall);
  const myGridRef = useRef<string>(myGrid);

  useEffect(() => {
    myCallRef.current = myCall;
  }, [myCall]);

  useEffect(() => {
    myGridRef.current = myGrid;
  }, [myGrid]);

  const modeRef = useRef<'FT8' | 'FT4'>(mode);
  useEffect(() => { modeRef.current = mode; }, [mode]);

  useEffect(() => { isTransmittingRef.current = isTransmitting; }, [isTransmitting]);

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const lastDrawTimeRef = useRef<number>(0);
  const waterfallRowsRef = useRef<number>(0);

  const drawWaterfall = useCallback((time: number) => {
    // TX Freeze Logic: completely freeze waterfall if Transmitting
    if (isTransmittingRef.current) return;

    // Throttle to 100ms (10fps). 1 pixel per frame = 10 px / sec.
    if (time - lastDrawTimeRef.current < 100) return;
    lastDrawTimeRef.current = time;

    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return;

    const width = canvas.width;
    const height = canvas.height;

    // Shift the canvas down 1px and stamp one horizontal row onto the new top
    // edge from the given 0..255 spectrum bins, using the existing palette
    // mapping (Black -> Blue -> Purple/Red -> Yellow/White).
    const shiftDownAndDrawRow = (bins: Uint8Array) => {
      // Shift current canvas image vertically downwards by 1px
      ctx.drawImage(canvas, 0, 0, width, height - 1, 0, 1, width, height - 1);

      // Compute the new top row
      const rowImg = ctx.createImageData(width, 1);
      for (let x = 0; x < width; x++) {
        // Ratio-map the server's bin count onto the canvas width (matches the
        // mobile PWA at server/web/static/js/waterfall.js); the `|| 0` guards a
        // NaN when the index falls outside a short bins array.
        const val = bins[Math.floor((x / width) * bins.length)] || 0;

        const px = x * 4;
        // Smooth color palette: Black -> Blue -> Purple/Red -> Yellow/White
        let r = 0, g = 0, b = 0;

        if (val < 50) {
          b = val * 2;
        } else if (val < 100) {
          b = 100 + (val - 50) * 3;
          r = (val - 50) * 2;
        } else if (val < 180) {
          b = 250 - (val - 100);
          r = 100 + (val - 100) * 1.5;
        } else {
          r = 255;
          g = (val - 180) * 3;
          b = (val - 220) * 5 > 0 ? (val - 220) * 5 : 0;
        }

        rowImg.data[px + 0] = Math.min(255, Math.max(0, r));
        rowImg.data[px + 1] = Math.min(255, Math.max(0, g));
        rowImg.data[px + 2] = Math.min(255, Math.max(0, b));
        rowImg.data[px + 3] = 255;
      }

      ctx.putImageData(rowImg, 0, 0);
    };

    // Drain ALL server WF01 frames buffered since the last draw (Task 8): one
    // horizontal row per frame from frame.bins, which the server maps 0–3000 Hz
    // across. When the buffer is empty we skip the shift entirely, so the
    // previous frame stays on screen instead of being blanked.
    const frames = waterfallRef.current.splice(0, waterfallRef.current.length);
    for (const frame of frames) {
      shiftDownAndDrawRow(frame.bins);
      // Advance the row counter (naturally pauses during TX since drawWaterfall
      // returns early).
      waterfallRowsRef.current += 1;
    }
  }, []);

  // Local audio capture was removed with the DSP brain (Task 6). audioActive
  // now mirrors the server connection; this button is a status lamp until
  // Task 10 rewires the connection UI.
  const toggleAudio = useCallback(async () => {
    // Audio input is managed on the station; nothing to toggle here.
  }, []);

  // Map the server state snapshot into the status UI (Task 10): VFO frequency
  // and station identity come from the server (not localStorage), the TX
  // queued/transmitting flags come from sequencer.tx_enabled + safety.ptt_on,
  // and the FSM label tracks the server QSO phase.
  useEffect(() => {
    setVfoFreq(snapshot.radio.freq_hz ?? vfoFreqRef.current);
    setMyCall(snapshot.station.my_call || myCallRef.current);
    setMyGrid(snapshot.station.my_grid || myGridRef.current);
    const inQso =
      snapshot.sequencer.tx_enabled &&
      snapshot.sequencer.state !== 'idle' &&
      snapshot.sequencer.state !== 'done';
    setIsTransmitting(snapshot.safety.ptt_on);
    setIsTxQueued(inQso); // TX happens on eligible slots while a QSO is active
    setFsmState(mapSequencerState(snapshot.sequencer.state));
  }, [snapshot]);

  // The server sequencer is the source of truth for TX arm state: mirror its
  // tx_enabled into the local toggle (Task 9 deferred item). The mirror only
  // re-runs when the server value actually changes, so an optimistic local arm
  // by flipping the toggle survives until CQ/Ans arms the server and the
  // snapshot confirms it.
  useEffect(() => {
    setTxEnabled(snapshot.sequencer.tx_enabled);
  }, [snapshot.sequencer.tx_enabled]);

  // Another session holds the control lease: CQ/Ans arm via the server and are
  // disabled. STOP stays enabled (NFR-038 — it needs no lease).
  const leaseBlocked = snapshot.lease.held && !snapshot.lease.mine;
  // Server AUDIO interlock latched (§15.5): the safety controller reports
  // faults as a sorted list of Interlock enum values ("audio" among them).
  const audioFault = snapshot.safety.faults.includes('audio');

  // Selecting never transmits (§15.6): pick a decode and the server holds the
  // selection; the Ans button replies to it.
  const handleSelectRow = useCallback(async (row: FT8DecodedMessage) => {
    const candidate = rowToCandidate(row);
    if (!candidate) return;
    setTargetCall(candidate.call);
    setSelectedCandidate(candidate);
    if (!(await ensureLease())) {
      setTxNotice('Control is held by another session');
      return;
    }
    const res = await mrrc.select(candidate);
    if (!res.ok && res.reason === 'lease_required') {
      setTxNotice('Control is held by another session');
    }
  }, [ensureLease]);

  // Single CQ: the server sequencer runs the whole QSO from one CQ.
  const handleCq = useCallback(async () => {
    if (!(await ensureLease())) {
      setTxNotice('Control is held by another session');
      return;
    }
    const res = await mrrc.cq(false);
    if (!res.ok && res.reason === 'lease_required') {
      setTxNotice('Control is held by another session');
    }
  }, [ensureLease]);

  // Reply to the selected decode (or a hand-entered call). The server returns
  // scheduled_tx; when the slot lands past the fit deadline it is deferred, so
  // tell the operator when the reply actually goes out.
  const handleAns = useCallback(async () => {
    const candidate = selectedCandidate ?? {
      call: targetCall,
      grid: '',
      snr: 0,
      text: targetCall,
      is_cq: false,
      slot_id: 0,
      freq: txFreq,
    };
    if (!candidate.call) return;
    if (!(await ensureLease())) {
      setTxNotice('Control is held by another session');
      return;
    }
    const res = await mrrc.reply(candidate);
    if (!res.ok) {
      setTxNotice(res.reason === 'lease_required'
        ? 'Control is held by another session'
        : `Reply rejected: ${res.reason ?? res.status}`);
      return;
    }
    const scheduled = res.body?.scheduled_tx as { utc?: string; deferred?: boolean } | undefined;
    if (scheduled?.deferred && scheduled.utc) {
      setTxNotice(`Reply armed → TX at ${scheduled.utc.slice(0, 2)}:${scheduled.utc.slice(2, 4)}:${scheduled.utc.slice(4, 6)} UTC`);
    }
  }, [ensureLease, selectedCandidate, targetCall, txFreq]);

  // STOP is unconditional and needs no lease (NFR-038).
  const handleStop = useCallback(async () => {
    await mrrc.stop();
  }, []);

  // TX-enable is the operator's arm consent: flipping it OFF disarms the
  // server (txOff); arming happens implicitly when CQ/Ans hit the server.
  const handleTxEnableToggle = useCallback(() => {
    const next = !txEnabled;
    if (txEnabled && snapshot.lease.mine) void mrrc.txOff();
    setTxEnabled(next);
  }, [txEnabled, snapshot.lease.mine]);

  // Auto-sequence maps to the server's auto_call_new_dxcc setting.
  const handleAutoSequenceToggle = useCallback(() => {
    const next = !autoSequence;
    setAutoSequence(next);
    void mrrc.putSetting('auto_call_new_dxcc', next);
  }, [autoSequence]);

  // Sync Interval Management & Animation Frame — UTC clock, slot window progress,
  // and the waterfall only. The decode trigger, queued-TX start, FSM drive, and
  // period markers were removed with the local brain (Task 6).
  useEffect(() => {
    let animationFrameId: number;

    const loop = (time: number) => {
      animationFrameId = requestAnimationFrame(loop);

      const now = new Date();
      const seconds = now.getUTCSeconds();
      const ms = now.getUTCMilliseconds();
      const totalSeconds = seconds + (ms / 1000);

      const PERIOD = modeRef.current === 'FT4' ? 7.5 : 15;
      const secondsInWindow = totalSeconds % PERIOD;

      setWindowProgress((secondsInWindow / PERIOD) * 100);
      setUtcTime(now.toISOString().substring(11, 19));

      if (audioActive) {
        drawWaterfall(time);
      }
    };

    animationFrameId = requestAnimationFrame(loop);

    return () => {
      cancelAnimationFrame(animationFrameId);
    };
  }, [audioActive, drawWaterfall]);

  const handleWaterfallClick = (e: React.MouseEvent<HTMLElement, MouseEvent>) => {
    if (!canvasRef.current) return;
    const rect = canvasRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const freq = Math.round((x / rect.width) * 2800) + 200;
    setTxFreq(Math.max(200, Math.min(3000, freq)));
  };

  if (loggedIn === false) {
    return <LoginView onLoggedIn={() => { setLoggedIn(true); }} />;
  }
  if (loggedIn === null) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-app text-text-main">
        <p className="text-xs uppercase tracking-widest text-text-muted">Loading…</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-app text-text-main font-sans flex flex-col p-4 select-none">
      
      {/* Header Pipeline */}
      <header className="flex flex-wrap items-center justify-between bg-panel border border-border-subtle rounded-lg p-4 mb-3 shadow-lg gap-4">
        <div className="flex items-center gap-6 flex-wrap">
          <div className="flex flex-col">
            <span className="text-[10px] uppercase tracking-widest text-text-muted mb-1">System Status</span>
            <div className="flex gap-2">
              <button 
                onClick={() => toggleAudio()}
                className={`px-3 py-1.5 border rounded text-[10px] font-bold transition-all flex items-center gap-2 uppercase tracking-widest ${
                  audioActive 
                    ? 'bg-btn border-[#4caf50] text-green-600 dark:text-[#4caf50] hover:border-red-500 hover:text-red-500'
                    : 'bg-btn border-border-input hover:border-[#4caf50] text-text-muted hover:text-green-600 dark:text-[#4caf50]'
                }`}
              >
                <div className={`w-2 h-2 rounded-full ${audioActive ? 'bg-green-600 dark:bg-[#4caf50] shadow-[0_0_8px_#4caf50]' : 'bg-[#2a2c31]'}`}></div>
                {audioActive ? "Audio Active" : "Activate Audio"}
              </button>
              <button
                onClick={() => setShowSettings(true)}
                className="px-2 border border-border-input bg-btn hover:bg-btn-hover rounded flex items-center justify-center text-text-muted hover:text-text-main transition-colors"
                title="Settings"
              >
                  <Settings size={14} />
              </button>
              <button
                onClick={() => setShowAbout(true)}
                className="px-2 border border-border-input bg-btn hover:bg-btn-hover rounded flex items-center justify-center text-text-muted hover:text-text-main transition-colors"
                title="About / Help"
              >
                  <HelpCircle size={14} />
              </button>
              {/* Lease status (Task 10): CONTROLLER when this session holds the
                  control lease, OBSERVER otherwise. */}
              <span
                className={`px-2 py-1.5 border rounded text-[10px] font-bold uppercase tracking-widest flex items-center gap-1.5 ${
                  snapshot.lease.mine
                    ? 'border-[#4caf50] bg-[#0f2e1b] text-green-600 dark:text-[#4caf50]'
                    : 'border-border-input bg-btn text-text-muted'
                }`}
                title={snapshot.lease.mine ? 'This session holds radio control' : 'No radio control (observer)'}
              >
                <div className={`w-2 h-2 rounded-full ${snapshot.lease.mine ? 'bg-green-600 dark:bg-[#4caf50] shadow-[0_0_8px_#4caf50]' : 'bg-[#2a2c31]'}`}></div>
                {snapshot.lease.mine ? 'Controller' : 'Observer'}
              </span>
            </div>
          </div>

          {/* Server AUDIO interlock lamp (Task 10): the VU meter was removed
              with the local audio brain (Task 6). Green = audio interlock
              healthy; red = the server latched an audio fault (snapshot
              safety.faults includes the "audio" Interlock value). */}
          <div className="flex flex-col">
            <span className="text-[10px] uppercase tracking-widest text-text-muted mb-1">Audio Interlock</span>
            <span
              className={`px-3 py-1.5 border rounded text-[10px] font-bold uppercase tracking-widest flex items-center gap-2 ${
                audioFault
                  ? 'border-red-700 bg-red-950/60 text-red-400'
                  : 'border-[#4caf50] bg-btn text-green-600 dark:text-[#4caf50]'
              }`}
              title={audioFault ? 'Server audio interlock FAULT' : 'Server audio interlock OK'}
            >
              <div className={`w-2 h-2 rounded-full ${audioFault ? 'bg-red-500 shadow-[0_0_8px_#ef4444] animate-pulse' : 'bg-green-600 dark:bg-[#4caf50] shadow-[0_0_8px_#4caf50]'}`}></div>
              {audioFault ? 'Audio Fault' : 'Audio OK'}
            </span>
          </div>
        </div>

        {/* --- RF Frequency Readout --- */}
        <div className="flex flex-col items-center justify-center min-w-[180px]">
          <span className="text-[10px] uppercase tracking-widest text-text-muted mb-1">Radio VFO</span>
          {editingVfo ? (
            <input
              type="text"
              className="text-[26px] font-mono font-bold leading-none tracking-tight text-green-600 dark:text-[#4caf50] bg-transparent border-b border-[#4caf50] outline-none w-[180px] text-center"
              value={vfoInputStr}
              autoFocus
              onChange={e => setVfoInputStr(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') commitVfoInput();
                if (e.key === 'Escape') setEditingVfo(false);
              }}
              onBlur={commitVfoInput}
            />
          ) : (
            <span
              className="text-[26px] font-mono font-bold leading-none tracking-tight cursor-pointer hover:opacity-70 transition-opacity text-green-600 dark:text-[#4caf50]"
              title="Click to enter custom frequency (MHz)"
              onClick={() => {
                setVfoInputStr((vfoFreq / 1_000_000).toFixed(6));
                setEditingVfo(true);
              }}
            >
              {formatFrequency(vfoFreq)}
            </span>
          )}
        </div>

        <div className="flex flex-col items-center">
          <span className="text-[10px] uppercase tracking-widest text-text-muted mb-1">{mode} Window ({mode === 'FT4' ? '7.5s' : '15s'} Sync)</span>
          <div className="w-32 md:w-48 h-1.5 bg-black rounded-full border border-border-subtle relative overflow-hidden">
             <div 
              className="absolute left-0 top-0 h-full bg-green-600 dark:bg-[#4caf50] transition-all duration-75 ease-linear shadow-[0_0_5px_rgba(76,175,80,0.5)]"
              style={{ width: `${windowProgress}%` }}
            />
          </div>
        </div>

        <div className="flex flex-col items-end min-w-[120px]">
          <span className="text-[10px] uppercase tracking-widest text-text-muted block mb-1">Station Clock (UTC)</span>
          <span className="text-2xl font-mono font-bold text-text-main leading-none">
            {utcTime}
          </span>
          <div className="flex items-center gap-1.5 mt-1.5" title={clockVerdict.message}>
            <span
              className={`h-2 w-2 rounded-full transition-colors duration-500 ${
                clockVerdict.status === 'ok'   ? 'bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.7)]' :
                clockVerdict.status === 'warn' ? 'bg-yellow-400 shadow-[0_0_6px_rgba(250,204,21,0.7)] animate-pulse' :
                clockVerdict.status === 'bad'  ? 'bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.7)] animate-pulse' :
                                                 'bg-gray-500'
              }`}
            />
            <span className={`text-[10px] font-mono tracking-wide ${
              clockVerdict.status === 'bad'  ? 'text-red-400' :
              clockVerdict.status === 'warn' ? 'text-yellow-400' :
              'text-text-muted'
            }`}>
              {clockVerdict.message}
            </span>
          </div>
        </div>
      </header>

      {/* --- Band Selection Bar --- */}
      <div className="bg-panel rounded-lg border border-border-subtle p-1.5 mb-3 flex items-center justify-between shadow-sm gap-2">
        <div 
          className="flex gap-2 overflow-x-auto band-control-bar flex-1 min-w-0 pr-4 lg:border-r border-border-subtle shrink"
          style={{ scrollbarWidth: 'none', msOverflowStyle: 'none' }}
        >
          <style>{`.band-control-bar::-webkit-scrollbar { display: none; }`}</style>
          {BAND_FREQS.map(band => {
            // If within 2kHz of standard FT8 frequency, consider it active
            const isActive = Math.abs(vfoFreq - band.hz) < 2000;
            return (
              <button
                key={band.label}
                onClick={() => selectBand(band.hz)}
                className={`px-5 py-1.5 rounded-full text-[11px] uppercase tracking-wider font-bold transition-colors whitespace-nowrap shrink-0 ${
                  isActive 
                    ? 'bg-blue-600 text-white shadow-[0_0_8px_rgba(37,99,235,0.8)] border border-blue-400' 
                    : 'bg-btn text-text-muted border border-transparent hover:bg-btn-hover hover:text-text-main'
                }`}
              >
                {band.label}
              </button>
            );
          })}
        </div>
        
        {/* Mode Toggle */}
        <button
          onClick={() => {
            const newMode = mode === 'FT8' ? 'FT4' : 'FT8';
            const freqs = newMode === 'FT4' ? BAND_FREQS_FT4 : BAND_FREQS_FT8;
            const currentBand = getBandFromFreq(vfoFreq);
            const match = freqs.find(b => b.label === currentBand);
            setMode(newMode);
            if (match) selectBand(match.hz);
          }}
          className={`shrink-0 px-4 py-1.5 rounded text-[11px] font-mono uppercase font-bold border transition-colors ${
            mode === 'FT8'
              ? 'bg-[#0f1e30] text-blue-400 border-blue-800 hover:bg-[#162540]'
              : 'bg-[#2a1505] text-orange-400 border-orange-800 hover:bg-[#3d2007]'
          }`}
        >
          {mode}
        </button>

        {/* PTT Period Toggle */}
        <button
          onClick={() => setTxPeriod(p => p === 0 ? 1 : 0)}
          className={`shrink-0 px-4 py-1.5 rounded text-[11px] font-mono uppercase font-bold border transition-colors ${
            txPeriod === 0
              ? 'bg-[#0f2e1b] text-green-400 border-green-800 hover:bg-[#154628]'
              : 'bg-[#3d1f05] text-amber-500 border-amber-700 hover:bg-[#5a2e07]'
          }`}
        >
          Tx: {txPeriod === 0 ? 'Even (:00)' : `Odd (${mode === 'FT4' ? ':07' : ':15'})`}
        </button>
      </div>

      {/* Main Working Environment */}
      <div className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-3 min-h-[400px]">
        
        {/* Left pane: Logs */}
        <section className="lg:col-span-5 flex flex-col gap-3 min-h-0">
          
          {/* Band Activity (Global Log) */}
          <div className="bg-panel border border-border-subtle rounded-lg flex flex-col h-[300px] max-h-[300px] shrink-0 overflow-hidden">
            <div className="bg-header border-b border-border-subtle px-3 py-2 flex justify-between items-center rounded-t-lg shrink-0">
              <h3 className="text-[11px] font-bold text-text-muted tracking-widest uppercase flex items-center gap-2">
                <Activity size={14} className="text-green-600 dark:text-[#4caf50]"/>
                Band Activity
              </h3>
              <button
                onClick={() => setRxLog([])}
                className="text-[10px] font-mono font-bold uppercase bg-red-950/80 text-red-100 hover:bg-red-900 border border-red-700 hover:border-red-500 px-2.5 py-1 rounded transition-all shrink-0 shadow-sm"
                title="Clear Band Activity Log"
              >
                Clear
              </button>
            </div>
            <div className="flex-1 font-mono text-[11px] overflow-hidden p-2 flex flex-col">
              <div className="grid grid-cols-[55px_40px_60px_1fr] gap-2 py-1 text-text-muted border-b border-border-subtle mb-2 uppercase text-[9px] shrink-0">
                <div>Time</div>
                <div>SNR</div>
                <div>Freq</div>
                <div>Message</div>
              </div>
              <div className="flex-1 overflow-y-auto space-y-0.5 pr-1">
                {rxLog.length === 0 && (
                    <div className="text-text-muted flex items-center justify-center h-full opacity-50 p-6 text-center text-xs">
                        Awaiting FT8 signals...<br/>(Audio decoded every 15s synced period)
                    </div>
                )}
                {rxLog.map((log, i) => {
                  if (log.isDivider) {
                    return (
                      <div key={i} className="flex items-center justify-center py-2 opacity-50">
                         <span className="text-[9px] font-mono tracking-widest text-text-muted">{log.message}</span>
                      </div>
                    );
                  }

                  return (
                    <div
                      key={i}
                      onClick={() => {
                        // Select the decode on the server (never transmits).
                        handleSelectRow(log);

                        // Auto-set TX period to the OPPOSITE of the caller's period
                        const callerPeriod = log.periodIndex !== undefined
                          ? log.periodIndex
                          : (() => {
                              const seconds = parseInt(log.time.substring(4, 6), 10);
                              const periodLen = mode === 'FT4' ? 7.5 : 15;
                              return Math.floor(seconds / periodLen) % 2;
                            })();
                        setTxPeriod(callerPeriod === 0 ? 1 : 0);
                      }}
                      className="grid grid-cols-[55px_40px_60px_1fr] gap-2 hover:bg-btn cursor-pointer p-1 rounded transition-colors group text-[11px] items-center"
                    >
                      <span className="text-zinc-500">{log.time}</span>
                      <span className={log.snr > -10 ? 'text-green-400' : 'text-red-400'}>{log.snr}</span>
                      <span className="text-blue-400">{log.freq}Hz</span>
                      <span className="text-text-main group-hover:text-text-highlight font-bold flex items-center flex-wrap">
                        {log.message}
                        {/* B4/N/W DXCC badges removed with the local DXCC service (Task 6);
                            Task 12 re-adds them from the server snapshot/decodes. */}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* QSO Window (Targeted Log) */}
          <div className="bg-qso border border-border-subtle rounded-lg flex flex-col h-[250px] max-h-[250px] shrink-0 overflow-hidden">
            <div className="bg-header border-b border-border-subtle px-3 py-2 flex justify-between items-center rounded-t-lg shrink-0">
              <h3 className="text-[11px] font-bold text-text-muted tracking-widest uppercase flex items-center gap-2">
                <Activity size={14} className="text-green-600 dark:text-[#4caf50]"/> 
                Active QSO
              </h3>
              <button
                onClick={() => setQsoLog([])}
                className="text-[10px] font-mono font-bold uppercase bg-red-950/80 text-red-100 hover:bg-red-900 border border-red-700 hover:border-red-500 px-2.5 py-1 rounded transition-all shrink-0 shadow-sm"
                title="Clear Active QSO Log"
              >
                Clear
              </button>
            </div>
            <div className="flex-1 font-mono text-[11px] overflow-hidden p-2 flex flex-col">
              <div className="grid grid-cols-[55px_40px_60px_1fr] gap-2 py-1 text-text-muted border-b border-border-subtle mb-2 uppercase text-[9px] shrink-0">
                <div>Time</div>
                <div>SNR</div>
                <div>Freq</div>
                <div>Message</div>
              </div>
              <div className="flex-1 overflow-y-auto space-y-0.5 pr-1">
                {qsoLog.length === 0 && (
                    <div className="text-text-muted flex items-center justify-center h-full opacity-50 p-6 text-center text-xs">
                        No active QSOs...
                    </div>
                )}
                {qsoLog.map((log, i) => {
                  if (log.isDivider) return null;
                  
                  let textClass = "text-text-main font-bold";
                  if (log.isTx) textClass = "text-sky-300 font-bold";
                  else if (log.isIncoming) textClass = "text-green-400 font-bold";
                  
                  return (
                  <div 
                    key={i}
                    onClick={() => {
                      // Select the decode on the server (never transmits).
                      if (!log.isTx) handleSelectRow(log);

                      // If incoming message, set TX period to OPPOSITE of caller's period
                      if (!log.isTx) {
                        const callerPeriod = log.periodIndex !== undefined
                          ? log.periodIndex
                          : log.time && log.time.length >= 6
                            ? (() => {
                                const seconds = parseInt(log.time.substring(4, 6), 10);
                                const periodLen = mode === 'FT4' ? 7.5 : 15;
                                return Math.floor(seconds / periodLen) % 2;
                              })()
                            : null;
                        if (callerPeriod !== null) setTxPeriod(callerPeriod === 0 ? 1 : 0);
                      }
                    }}
                    className="grid grid-cols-[55px_40px_60px_1fr] gap-2 hover:bg-btn cursor-pointer p-1 rounded transition-colors group text-[11px] items-center"
                  >
                    <span className="text-zinc-500">{log.time}</span>
                    <span className={log.isTx ? 'text-zinc-500' : (log.snr > -10 ? 'text-green-400' : 'text-red-400')}>{log.isTx ? '--' : log.snr}</span>
                    <span className="text-blue-400">{log.freq}Hz</span>
                    <span className={`group-hover:text-text-highlight ${textClass} flex items-center flex-wrap`}>
                      {log.message}
                      {/* N/W DXCC badges removed with the local DXCC service (Task 6);
                          Task 12 re-adds them from the server snapshot/decodes. */}
                    </span>
                  </div>
                  );
                })}
              </div>
            </div>
          </div>
        </section>

        {/* Right pane: Waterfall DSP */}
        <section className={`lg:col-span-7 border border-border-subtle rounded-lg overflow-hidden flex flex-col relative h-[300px] lg:h-auto ${theme === 'dark' ? 'bg-[#050505]' : 'bg-white'}`}>
           <div className="absolute top-0 inset-x-0 bg-black/40 backdrop-blur-sm px-3 py-1 border-b border-border-subtle flex justify-between z-10 pointer-events-none">
            <span className={`text-[9px] font-mono tracking-tighter ${theme === 'dark' ? 'text-[#4caf50]' : 'text-green-600'}`}>WATERFALL (200 - 3000 Hz)</span>
            <div className="flex gap-4">
               <span className="text-[9px] font-mono text-zinc-500">1k</span>
               <span className="text-[9px] font-mono text-zinc-500">2k</span>
               <span className="text-[9px] font-mono text-zinc-500">3k</span>
            </div>
          </div>
          <div className="flex-1 relative bg-black flex items-end">
            <canvas 
               ref={canvasRef}
               width={1024} 
               height={300} 
               className="w-full h-full block cursor-crosshair"
               onClick={handleWaterfallClick}
            />
            {/* TX Frequency Overlay Bar (50 Hz for FT8, 83 Hz for FT4) */}
            <div
               className="absolute top-0 bottom-0 bg-red-500/35 border-x border-red-500/50 pointer-events-none transition-all duration-75"
               style={{
                 left: `${((txFreq - 200) / 2800) * 100}%`,
                 width: `${((mode === 'FT4' ? 83.33 : 50) / 2800) * 100}%`,
                 transform: 'translateX(-50%)'
               }}
            />
            {/* Waterfall Static Vertical Rule */}
            <div className="absolute inset-0 flex pointer-events-none">
               <div className="h-full w-px bg-[#2a2c31]/30 ml-[33.3%]"></div>
               <div className="h-full w-px bg-[#2a2c31]/30 ml-[33.3%]"></div>
            </div>
            {!audioActive && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/60 font-bold text-zinc-500 z-20 text-xs">
                    AUDIO INACTIVE
                </div>
            )}
          </div>
        </section>
      </div>

      {/* TX Operations Panel */}
      <footer className="bg-header border border-border-subtle rounded-lg p-4 shadow-inner mt-3">
        <div className="flex flex-col lg:flex-row gap-4 items-center justify-between">
          
          <div className="w-full lg:w-auto space-y-3 lg:border-r border-border-subtle lg:pr-6 shrink-0">
            <div className="flex flex-col">
              <label className="text-[9px] uppercase tracking-widest text-text-muted mb-1">My Station</label>
              <div className="flex items-center gap-2 cursor-pointer" onClick={() => setShowSettings(true)}>
                <span className={`border border-border-subtle rounded px-3 py-1.5 text-xs font-mono uppercase font-bold min-w-[80px] text-center ${
                  theme === 'dark' ? 'bg-[#050505] text-[#4caf50]' : 'bg-white text-green-600'
                }`} title="Click to edit in Settings">{myCall}</span>
                <span className={`border border-border-subtle rounded px-3 py-1.5 text-xs font-mono uppercase min-w-[60px] text-center ${
                  theme === 'dark' ? 'bg-[#050505] text-text-muted' : 'bg-white text-text-muted'
                }`} title="Click to edit in Settings">{myGrid}</span>
                <span className={`border border-border-subtle rounded px-3 py-1.5 text-xs font-mono min-w-[80px] text-center ${
                  theme === 'dark' ? 'bg-[#050505] text-text-main' : 'bg-white text-text-main'
                }`} title="Click to edit in Settings">{txFreq} Hz</span>
              </div>
            </div>
            <div className="flex flex-col">
              <label className="text-[9px] uppercase tracking-widest text-text-muted mb-1">Target Station</label>
              <input type="text" value={targetCall} placeholder="DX_CALL" onChange={e => { setSelectedCandidate(null); setTargetCall(e.target.value.toUpperCase()); }} className="bg-app border border-border-input rounded px-2 py-1 text-xs font-mono w-full max-w-[200px] focus:outline-none focus:border-blue-500 text-text-main uppercase" />
            </div>
          </div>

          <div className="w-full lg:w-auto flex-1 grid grid-cols-2 gap-2 px-0 lg:px-4">
             <button
                onClick={handleCq}
                disabled={(!autoSequence && !txEnabled) || isTransmitting || isTxQueued || leaseBlocked}
                className="h-10 bg-btn border border-border-input hover:bg-btn-hover disabled:opacity-50 disabled:hover:bg-btn text-[10px] font-bold rounded uppercase tracking-wider transition-colors flex items-center justify-center gap-1"
                title="Send a single CQ (server sequencer runs the whole QSO)"
              >
                 CQ {myCall}
             </button>

             <button
                onClick={handleAns}
                disabled={(!autoSequence && !txEnabled) || !targetCall || isTransmitting || isTxQueued || leaseBlocked}
                className="h-10 bg-btn border border-border-input hover:bg-btn-hover disabled:opacity-50 disabled:hover:bg-btn text-[10px] font-bold rounded uppercase tracking-wider transition-colors flex items-center justify-center gap-1"
                title="Reply to the selected station"
              >
                 Ans {targetCall || '...'}
             </button>

             {/* STOP is unconditional and needs no lease (NFR-038). */}
             <button
                onClick={handleStop}
                className="col-span-2 h-10 bg-red-950/70 border border-red-700 hover:bg-red-900 text-red-100 text-[10px] font-bold rounded uppercase tracking-wider transition-colors flex items-center justify-center gap-1.5"
                title="Stop transmitting (works even without control)"
              >
                 <Square size={12} /> Stop TX
             </button>

             {(isTransmitting || isTxQueued) && (
                <div className={`col-span-2 mt-1 flex items-center justify-center gap-2 p-2 ${isTransmitting ? 'bg-rose-950/40 text-rose-400 border-rose-900 animate-pulse' : 'bg-amber-950/40 text-amber-500 border-amber-900'} border rounded font-bold text-[10px] uppercase tracking-widest`}>
                    <Activity size={12} className={isTransmitting ? "" : "opacity-50"} /> {isTransmitting ? 'Transmitting...' : 'TX Queued...'}
                </div>
             )}
          </div>

          <div className="w-full lg:w-auto flex flex-col sm:flex-row gap-3 items-center justify-center lg:pl-6 lg:border-l border-border-subtle mt-4 lg:mt-0 shrink-0">
            {/* Auto Sequence Toggle and FSM State Indicator */}
            <div className="flex flex-col items-center justify-center">
              <button
                 onClick={handleAutoSequenceToggle}
                 className={`w-full lg:w-32 h-16 border rounded flex flex-col items-center justify-center gap-1 group transition-all active:scale-95 ${
                   autoSequence 
                     ? (theme === 'dark'
                         ? 'bg-[#0f2a18] border-[#184525] hover:bg-[#153a21] text-green-400 font-bold'
                         : 'bg-green-100 border-green-300 hover:bg-green-200 text-green-800 font-bold')
                     : 'bg-btn border-border-input hover:bg-btn-hover text-text-muted'
                 }`}
              >
                 <span className="text-[9px] font-bold tracking-widest uppercase">AUTO SEQUENCE</span>
                 <span className="text-[10px] font-mono uppercase bg-black/40 px-2 py-0.5 rounded text-sky-400 font-bold tracking-wider">
                   {autoSequence ? fsmState : "OFF"}
                 </span>
              </button>
              {autoSequence && fsmQueue.length > 0 && (
                <div className="w-full lg:w-32 mt-1 max-h-[72px] overflow-y-auto flex flex-col gap-0.5 custom-scrollbar">
                  {fsmQueue.map(c => (
                    <div key={c.callsign} className="flex items-center justify-between px-1.5 py-0.5 rounded bg-black/30 border border-border-subtle/40">
                      <span className="text-[9px] font-mono font-bold text-sky-400 tracking-wide">{c.callsign}</span>
                      <span className="text-[9px] font-mono text-zinc-400">
                        {c.report ?? '?'}{c.distance ? ` ${c.distance}km` : ''}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Enable TX PTT Trigger */}
            <div className="flex flex-col items-center justify-center">
              <button
                 onClick={handleTxEnableToggle}
                 className={`w-full lg:w-32 h-16 border rounded flex flex-col items-center justify-center gap-1 group transition-all active:scale-95 ${
                   txEnabled 
                     ? (theme === 'dark'
                         ? 'bg-[#2a0e0e] border-[#4a1a1a] hover:bg-[#3a1212] text-[#ff4444]'
                         : 'bg-red-100 border-red-300 hover:bg-red-200 text-red-600')
                     : 'bg-btn border-border-input hover:bg-btn-hover text-text-muted'
                 }`}
              >
                 <span className="text-[10px] font-bold tracking-widest uppercase">{txEnabled ? 'TX Enabled' : 'Enable TX'}</span>
                 <div className={`w-8 h-2 rounded-full relative ${
                   txEnabled 
                     ? (theme === 'dark' ? 'bg-[#4a1a1a]' : 'bg-red-200')
                     : 'bg-panel'
                 }`}>
                   <div className={`absolute left-0 top-0 w-3 h-2 rounded-full transition-all ${
                     txEnabled 
                       ? (theme === 'dark' 
                           ? 'bg-[#ff4444] shadow-[0_0_8px_#ff4444] translate-x-5' 
                           : 'bg-red-600 shadow-[0_0_8px_rgba(220,38,38,0.5)] translate-x-5')
                       : 'bg-[#3a3d45]'
                   }`}></div>
                 </div>
              </button>
              
            </div>
          </div>
        </div>
      </footer>

      {/* Logbook Viewer Section */}
      <div className="w-full mt-3">
         <LogBookViewer
            maxEntries={maxLogEntries}
            wavelogEnabled={false}
            wavelogUrl={''}
            wavelogApiKey={''}
            wavelogStationProfileId={''}
         />
      </div>

      {showSettings && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex flex-col items-center justify-center p-4">
          <div className="bg-panel border border-border-subtle p-6 rounded-lg shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-sm font-bold uppercase tracking-widest text-text-main">Station Configuration</h2>
              <button onClick={() => setShowSettings(false)} className="text-text-muted hover:text-text-main">
                <X size={20} />
              </button>
            </div>
            
            <div className="space-y-4">
              <div className="flex flex-col gap-1">
                <label className="text-[10px] uppercase tracking-widest text-text-muted">My Callsign</label>
                <input type="text" value={myCall} onChange={e => setMyCall(e.target.value.toUpperCase())} className="bg-app border border-border-input rounded px-3 py-2 text-sm font-mono w-full focus:outline-none focus:border-[#4caf50] text-text-main uppercase" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[10px] uppercase tracking-widest text-text-muted">My Grid Locator</label>
                <input type="text" value={myGrid} onChange={e => setMyGrid(e.target.value.toUpperCase())} className="bg-app border border-border-input rounded px-3 py-2 text-sm font-mono w-full focus:outline-none focus:border-[#4caf50] text-text-main uppercase" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[10px] uppercase tracking-widest text-text-muted">Decoder Depth</label>
                <select 
                  value={decodeDepth.toString()}
                  onChange={e => setDecodeDepth(Number(e.target.value))}
                  className="bg-app border border-border-input text-text-main rounded px-3 py-2 text-xs font-mono w-full focus:outline-none focus:border-[#4caf50]"
                >
                  <option value="1">1 - Fast (Normal)</option>
                  <option value="2">2 - Deep (Slower, Decodes more)</option>
                  <option value="3">3 - Max (Slowest, Decodes weak signals)</option>
                </select>
              </div>

              <div className="flex flex-col gap-1">
                <label className="text-[10px] uppercase tracking-widest text-text-muted">Max Retries</label>
                <input 
                  type="number" 
                  min="0"
                  max="10"
                  value={maxRetries} 
                  onChange={e => setMaxRetries(Number(e.target.value))} 
                  className="bg-app border border-border-input rounded px-3 py-2 text-sm font-mono w-full focus:outline-none focus:border-[#4caf50] text-text-main" 
                />
              </div>

              <div className="flex flex-col gap-1">
                <label className="text-[10px] uppercase tracking-widest text-text-muted">Final Message Mode</label>
                <select 
                  value={finalMessageMode}
                  onChange={e => setFinalMessageMode(e.target.value as 'RR73' | 'RRR')}
                  className="bg-app border border-border-input text-text-main rounded px-3 py-2 text-xs font-mono w-full focus:outline-none focus:border-[#4caf50]"
                >
                  <option value="RR73">RR73 (Standard, Faster)</option>
                  <option value="RRR">RRR (Requires 73 from target)</option>
                </select>
              </div>

              <div className="flex items-center justify-between pt-2">
                 <div className="flex flex-col">
                    <label className="text-[10px] uppercase tracking-widest text-text-muted">Skip Grid (TX1)</label>
                    <span className="text-[9px] text-text-muted">Answer a CQ with the report, not the grid</span>
                 </div>
                 <button
                    onClick={() => setSkipTx1Grid(!skipTx1Grid)}
                    className={`bg-app border rounded px-3 py-1 text-xs font-mono focus:outline-none transition-colors ${skipTx1Grid ? 'border-[#4caf50] text-[#4caf50]' : 'border-border-input text-text-main'}`}
                 >
                    {skipTx1Grid ? 'Enabled' : 'Disabled'}
                 </button>
              </div>

              <div className="flex flex-col gap-1">
                <label className="text-[10px] uppercase tracking-widest text-text-muted">Display Last N QSOs</label>
                <input 
                  type="number" 
                  min="10"
                  max="1000"
                  value={maxLogEntries} 
                  onChange={e => setMaxLogEntries(Number(e.target.value))} 
                  className="bg-app border border-border-input rounded px-3 py-2 text-sm font-mono w-full focus:outline-none focus:border-[#4caf50] text-text-main" 
                />
              </div>

              {/* CAT Radio Control settings removed with the local brain (Task 6);
                  Task 13 re-adds a server-driven radio section. */}

              <div className="flex items-center justify-between pt-2">
                 <label className="text-[10px] uppercase tracking-widest text-text-muted">Color Scheme</label>
                 <button 
                    onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                    className="bg-app border border-border-input text-text-main rounded px-3 py-1 text-xs font-mono focus:outline-none hover:border-[#4caf50]"
                 >
                    {theme === 'dark' ? 'Dark Mode' : 'Light Mode'}
                 </button>
              </div>

              <div className="flex items-center justify-between pt-2">
                 <div className="flex flex-col">
                    <label className="text-[10px] uppercase tracking-widest text-text-muted">DXCC Ignore Mode</label>
                    <span className="text-[9px] text-text-muted">Count DXCC/callsigns as worked per band, any mode</span>
                 </div>
                 <button
                    onClick={() => setDxccIgnoreMode(!dxccIgnoreMode)}
                    className={`bg-app border rounded px-3 py-1 text-xs font-mono focus:outline-none transition-colors ${dxccIgnoreMode ? 'border-[#4caf50] text-[#4caf50]' : 'border-border-input text-text-main'}`}
                 >
                    {dxccIgnoreMode ? 'Enabled' : 'Disabled'}
                 </button>
              </div>

              {/* Cloudlog/Wavelog, External Data Stream, PSKReporter and Audio Input/Output
                  settings removed with the local brain (Task 6); Task 13 re-adds a
                  server-driven settings section. */}
            </div>
            
            <div className="mt-8 flex justify-end">
                <button 
                  onClick={() => setShowSettings(false)}
                  className="bg-green-600 dark:bg-[#4caf50] hover:bg-green-600 text-black px-6 py-2 rounded text-xs font-bold uppercase tracking-widest"
                >
                  Save & Close
                </button>
            </div>
          </div>
        </div>
      )}

      {showAbout && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex flex-col items-center justify-center p-4">
          <div className="bg-panel border border-border-subtle p-6 rounded-lg shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center mb-3">
              <h2 className="text-sm font-bold uppercase tracking-widest text-text-main">About FT8 Web Client</h2>
              <button onClick={() => setShowAbout(false)} className="text-text-muted hover:text-text-main">
                <X size={20} />
              </button>
            </div>
            <div className="flex flex-wrap gap-2 mb-5">
              <a
                href="https://buymeacoffee.com/ok1cdj"
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 bg-[#FFDD00] hover:bg-[#ffea42] text-black font-extrabold px-3 py-1.5 rounded transition-transform hover:scale-102 active:scale-98 shadow-sm text-[11px] uppercase tracking-wide cursor-pointer"
              >
                <span className="text-sm">☕</span>
                <span>Buy me a coffee</span>
              </a>
              <a
                href="https://github.com/ok1cdj/FT8web"
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 bg-btn border border-border-input hover:border-text-main text-text-muted hover:text-text-main px-3 py-1.5 rounded text-[11px] uppercase tracking-wide transition-colors cursor-pointer"
              >
                <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4" />
                  <path d="M9 18c-4.51 2-5-2-7-2" />
                </svg>
                <span>Report Issue</span>
              </a>
            </div>
            
            <div className="space-y-4 text-xs text-text-main leading-relaxed">
              <p>Welcome to the Web FT8 Client! This application runs entirely in your browser using the Web Audio API.</p>
              
              <div>
                <h3 className="font-bold text-green-600 dark:text-[#4caf50] mb-1">1. Audio Setup</h3>
                <p>Click "ACTIVATE AUDIO" and allow browser permissions to use the microphone. Adjust your computer's input volume so the <strong>Input Level (VU)</strong> sits in the green/yellow zone, avoiding the red to prevent clipping.</p>
              </div>

              <div>
                <h3 className="font-bold text-green-600 dark:text-[#4caf50] mb-1">2. Configuration</h3>
                <p>Open <strong>Settings</strong> to set your Call Sign, Grid Square, and CAT control.</p>
              </div>

              <div>
                <h3 className="font-bold text-green-600 dark:text-[#4caf50] mb-1">3. Operations</h3>
                <p>Select your band using the pill buttons and choose <strong>FT8</strong> or <strong>FT4</strong> mode. FT8 decodes at :00, :15, :30, :45; FT4 decodes at :00, :07, :15, :22, :30, :37, :45, :52.</p>
                <p>Both modes rely strictly on synchronized UTC time — verify your system clock is accurate.</p>
                <p>Each decoded callsign shows a DXCC entity badge and an <strong>N</strong> (new) or <strong>W</strong> (worked) indicator for the current band and mode.</p>
                <p>Enable TX and the FSM will automatically manage CQ, grid exchange, SNR report, and 73.</p>
              </div>

              <div>
                <h3 className="font-bold text-green-600 dark:text-[#4caf50] mb-1">4. Wavelog & Cloudlog Integration</h3>
                <p>You can seamlessly log your completed QSOs directly to your <strong>Wavelog</strong> or <strong>Cloudlog</strong> instance. In Settings, enable the integration and input your instance URL, API Key, and Station Profile ID.</p>
                <p>QSOs are uploaded in real time. If an upload fails, you can use the **Sync All** button or click the manual cloud-upload action icon next to a specific logbook entry to re-trigger the upload. Troubleshooting output will exist in the browser developer tools console.</p>
              </div>

              <div className="pt-4 mt-4 border-t border-border-subtle">
                <h3 className="font-bold text-green-600 dark:text-[#4caf50] mb-1">Tested Radios & Feedback</h3>
                <p className="mb-2"><strong>Tested:</strong> IC-705, IC-7300, Yaesu FTX-1, Flex 6400, Flex 8400. FT-817 in progress.</p>
                <p className="mb-2">If you have success with your radio model — or hit a protocol issue — please report it on GitHub!</p>
              </div>

              <div className="pt-4 mt-4 border-t border-border-subtle">
                <h3 className="font-bold text-green-600 dark:text-[#4caf50] mb-1">Credits & License</h3>
                <p className="mb-2">Created by <strong>Ondřej Koloničný, OK1CDJ</strong>.</p>
                <p className="mb-2">This project is open-source and licensed under the <strong>GNU General Public License v3 (GPL v3)</strong>.</p>
                <h4 className="font-bold text-text-main mt-4 mb-2">Acknowledgments</h4>
                <ul className="list-disc pl-5 mb-4 space-y-1 text-[11px] text-text-muted">
                  <li><strong className="text-text-main">FT8/FT4 Protocols:</strong> FT8 and FT4 are digital amateur radio modes designed for weak-signal communication, originally developed by <strong>Joe Taylor (K1JT)</strong> and <strong>Steve Franke (K9AN)</strong> as part of the WSJT-X suite.</li>
                  <li><strong className="text-text-main">DSP Implementation:</strong> This application utilizes the pure TypeScript DSP library <a href="https://github.com/e04/ft8ts" target="_blank" rel="noreferrer" className="text-blue-400 hover:text-blue-300 underline font-semibold">@e04/ft8ts</a>.</li>
                </ul>
              </div>
            </div>

            <div className="mt-4 pt-3 border-t border-border-subtle/40">
              <p className="text-[9px] text-text-muted leading-relaxed">
                The algorithms, source code, look-and-feel of WSJT-X and related programs, and protocol specifications for the modes FSK441, FST4, FST4W, FT4, FT8, JT4, JT6M, JT9, JT44, JT65, JTMS, Q65, QRA64, ISCAT, and MSK144 are Copyright © 2001-2026 by one or more of the following authors: Joseph Taylor, K1JT; Bill Somerville, G4WJS; Steven Franke, K9AN; Nico Palermo, IV3NWV; Uwe Risse, DG2YCB; Brian Moran, N9ADG; John Nelson, G4KLA; Charles Suckling, DL3WDG; Roger Rehr, W3SZ; Greg Beam, KI7MT; Michael Black, W9MDB; Edson Pereira, PY2SDR; Philip Karn, KA9Q; and other members of the WSJT Development Group.
              </p>
            </div>

            <div className="mt-4 flex justify-end">
                <button 
                  onClick={() => setShowAbout(false)}
                  className="bg-app border border-border-input hover:bg-btn text-text-main px-6 py-2 rounded text-xs font-bold uppercase tracking-widest transition-colors"
                >
                  Close
                </button>
            </div>
          </div>
        </div>
      )}
      
      {whatsNewEntries.length > 0 && (
        <WhatsNewModal entries={whatsNewEntries} onClose={closeWhatsNew} />
      )}

      <VersionInfo />

      {/* Inline TX notice (Task 9): deferred-reply slot and lease-blocked
          controls. Minimal replacement for a toast component. */}
      {txNotice && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[60] px-4 py-2 rounded-lg bg-panel border border-border-subtle text-text-main text-xs font-mono font-bold shadow-2xl whitespace-nowrap">
          {txNotice}
        </div>
      )}
    </div>
  );
}
