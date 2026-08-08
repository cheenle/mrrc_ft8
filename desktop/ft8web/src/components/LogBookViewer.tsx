import React, { useEffect, useState } from 'react';
import { dxccService } from '../services/DxccService';
import { mrrc } from '../services/mrrcClient';

// Row shape rendered by the logbook table. Built from the server's canonical
// QSO view (/logs/qsos); the server is authoritative, so rows are read-only.
interface LogRow {
    id: number;
    call: string;
    qso_date: string;
    time_on: string;
    band: string;
    mode: string;
    freq: number;
    gridsquare: string;
    rst_sent: string;
    rst_rcvd: string;
}

// The server stores reports as signed integers (-10, +07, ...). Format them the
// same way the server's ADIF export does ("%+03d") so the table matches ADIF.
function fmtRst(r: number | null | undefined): string {
    if (r == null) return '';
    return r >= 0 ? `+${String(Math.abs(r)).padStart(2, '0')}` : String(r);
}

// Map one server QSO to a viewer row. started_utc is HHMMSS-only (no date), so
// both the date and time come from completed_epoch (UTC), matching the server's
// canonical generate_adif() derivation in server/engine/adif.py.
function qsoToRow(q: any): LogRow {
    const utc = new Date((q.completed_epoch ?? 0) * 1000).toISOString();
    return {
        id: q.id,
        call: q.dx_call ?? '',
        qso_date: utc.slice(0, 10).replace(/-/g, ''),
        time_on: utc.slice(11, 19).replace(/:/g, ''),
        band: q.band ?? '',
        mode: q.mode ?? 'FT8',
        freq: (q.freq_hz ?? 0) / 1e6,
        gridsquare: q.dx_grid ?? '',
        rst_sent: fmtRst(q.report_sent),
        rst_rcvd: fmtRst(q.report_rcvd),
    };
}

export function LogBookViewer({ maxEntries }: { maxEntries: number }) {
    const [qsos, setQsos] = useState<LogRow[]>([]);
    const [filterCall, setFilterCall] = useState('');
    const [filterBand, setFilterBand] = useState('');
    const [filterMode, setFilterMode] = useState('');

    const fetchQsos = async () => {
        try {
            const data = await mrrc.qsos();
            const rows = (data.body.qsos ?? []).map(qsoToRow);
            setQsos(rows);
        } catch (e) {
            console.error("Failed to load QSOs", e);
        }
    };

    const hasFilter = filterCall.trim() !== '' || filterBand !== '' || filterMode !== '';

    const filteredQsos = hasFilter
        ? qsos.filter(q => {
            if (filterCall.trim() && !q.call.toUpperCase().includes(filterCall.trim().toUpperCase())) return false;
            if (filterBand && q.band !== filterBand) return false;
            if (filterMode && q.mode !== filterMode) return false;
            return true;
        })
        : qsos.slice(0, maxEntries);

    useEffect(() => {
        fetchQsos();
        // Periodically refresh so completed QSOs appear without a manual reload.
        const interval = setInterval(fetchQsos, 5000);
        return () => {
            clearInterval(interval);
        };
    }, [maxEntries]);

    return (
        <div className="logbook-vessel flex flex-col bg-panel border gap-2 border-border-subtle rounded mt-2 px-1">
            <div className="flex justify-between items-center shrink-0 py-2 pt-3 px-3">
                <div className="flex items-center gap-3">
                    <h3 className="text-xs font-bold text-text-main uppercase tracking-widest text-[#4caf50]">QSO Logbook</h3>
                    <span className="text-[10px] font-mono text-text-muted">{qsos.length} QSO</span>
                </div>
                <div className="flex items-center gap-1.5 flex-wrap">
                    <a
                        href="/api/v1/logs/adif"
                        download
                        className="bg-btn border border-border-input hover:bg-btn-hover hover:border-[#4caf50] hover:text-[#4caf50] text-[10px] font-bold px-3 py-1.5 rounded uppercase tracking-wider text-text-main transition-colors shadow-sm cursor-pointer inline-flex items-center"
                        title="Download the server logbook as ADIF"
                    >
                        Export ADIF
                    </a>
                </div>
            </div>
            <div className="flex items-center gap-2 px-3 pb-2 shrink-0 flex-wrap">
                <div className="relative">
                    <svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="absolute left-2 top-1/2 -translate-y-1/2 text-text-muted pointer-events-none">
                        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                    </svg>
                    <input
                        type="text"
                        placeholder="Search call..."
                        value={filterCall}
                        onChange={e => setFilterCall(e.target.value)}
                        className="bg-btn border border-border-input rounded pl-6 pr-2 py-1 text-[11px] text-text-main outline-none focus:border-[#4caf50] transition-colors w-32 uppercase placeholder:normal-case placeholder:text-text-muted"
                    />
                </div>
                <select
                    value={filterBand}
                    onChange={e => setFilterBand(e.target.value)}
                    className="bg-btn border border-border-input rounded px-2 py-1 text-[11px] text-text-main outline-none focus:border-[#4caf50] transition-colors cursor-pointer"
                >
                    <option value="">All Bands</option>
                    {['160m','80m','60m','40m','30m','20m','17m','15m','12m','10m','6m','2m'].map(b => (
                        <option key={b} value={b}>{b}</option>
                    ))}
                </select>
                <select
                    value={filterMode}
                    onChange={e => setFilterMode(e.target.value)}
                    className="bg-btn border border-border-input rounded px-2 py-1 text-[11px] text-text-main outline-none focus:border-[#4caf50] transition-colors cursor-pointer"
                >
                    <option value="">All Modes</option>
                    <option value="FT8">FT8</option>
                    <option value="FT4">FT4</option>
                </select>
                {hasFilter && (
                    <button
                        onClick={() => { setFilterCall(''); setFilterBand(''); setFilterMode(''); }}
                        className="text-[10px] font-bold px-2 py-1 rounded uppercase tracking-wider bg-btn border border-border-input text-text-muted hover:text-text-main hover:bg-btn-hover transition-colors cursor-pointer"
                    >
                        Clear
                    </button>
                )}
                {hasFilter && (
                    <span className="text-[10px] text-text-muted ml-auto">{filteredQsos.length} result{filteredQsos.length !== 1 ? 's' : ''}</span>
                )}
            </div>
            <div className="overflow-y-auto flex-1 text-xs px-3 pb-3 h-48 custom-scrollbar">
                {qsos.length === 0 ? (
                    <div className="text-center text-text-muted mt-8 italic text-[11px]">No QSOs logged yet.</div>
                ) : filteredQsos.length === 0 ? (
                    <div className="text-center text-text-muted mt-8 italic text-[11px]">No QSOs match the current filter.</div>
                ) : (
                    <div className="w-full">
                        <div className="sticky top-0 bg-panel text-text-muted text-[10px] uppercase tracking-wider grid grid-cols-[130px_100px_50px_40px_40px_40px_60px_120px] gap-2 pb-2 mb-1 border-b border-border-subtle z-10 font-bold text-left">
                            <div className="text-left">Date/Time (UTC)</div>
                            <div className="text-left">Call</div>
                            <div className="text-left">Band</div>
                            <div className="text-left">Mode</div>
                            <div className="text-left">Sent</div>
                            <div className="text-left">Rcvd</div>
                            <div className="text-left">Grid</div>
                            <div className="text-left hidden sm:block">DXCC</div>
                        </div>
                        <div className="flex flex-col">
                            {filteredQsos.map(qso => (
                                <div key={qso.id} className="grid grid-cols-[130px_100px_50px_40px_40px_40px_60px_120px] gap-2 py-1.5 border-b border-border-subtle/30 hover:bg-btn transition-colors items-center text-[11px] text-left">
                                    <div className="font-mono text-text-muted truncate text-left">{qso.qso_date} {qso.time_on}</div>
                                    <div className="font-bold text-sky-600 dark:text-sky-400 truncate tracking-wide text-left flex items-center gap-1.5 min-w-0">
                                        <span className="truncate">{qso.call}</span>
                                    </div>
                                    <div className="text-text-muted text-left">{qso.band}</div>
                                    <div className={`font-mono font-bold text-left text-[10px] ${qso.mode === 'FT4' ? 'text-orange-400' : 'text-blue-400'}`}>{qso.mode || 'FT8'}</div>
                                    <div className="text-green-600 dark:text-[#4caf50] font-mono text-left">{qso.rst_sent}</div>
                                    <div className="text-red-650 dark:text-red-450 font-mono text-left">{qso.rst_rcvd}</div>
                                    <div className="text-zinc-600 dark:text-zinc-400 font-mono tracking-wider text-left">{qso.gridsquare || '-'}</div>
                                    <div className="text-zinc-500 dark:text-zinc-400 text-left text-[10px] hidden sm:block truncate" title={(() => { const e = dxccService.lookup(qso.call); return e?.name; })()}>
                                        {(() => { const e = dxccService.lookup(qso.call); return e ? (e.name.length > 14 ? e.name.substring(0, 13) + '…' : e.name) : '-'; })()}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
