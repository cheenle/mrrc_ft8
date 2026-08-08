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
