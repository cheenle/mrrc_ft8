/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import {defineConfig} from 'vite';

export default defineConfig(() => {
  const commitHash = process.env.VERCEL_GIT_COMMIT_SHA 
    ? process.env.VERCEL_GIT_COMMIT_SHA.substring(0, 7) 
    : 'local-dev';
  const buildTime = new Date().toLocaleString('cs-CZ', { timeZone: 'Europe/Prague' });

  return {
    // The server mounts the built client at /desktop (server/main.py
    // _desktop_dist_dir); all asset URLs must resolve under that base path.
    base: '/desktop/',
    define: {
      __COMMIT_HASH__: JSON.stringify(commitHash),
      __BUILD_TIME__: JSON.stringify(buildTime),
    },
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    server: {
      port: 3000,
      proxy: {
        // Dev server forwards API + WS traffic to the loopback MRRC-FT8 server.
        '/api': 'http://127.0.0.1:8000',
        '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
      },
    },
    test: {
      environment: 'node',
      include: ['src/**/*.test.ts'],
    },
  };
});
