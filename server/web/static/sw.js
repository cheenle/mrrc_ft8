// App-shell service worker. API and WebSocket traffic is NEVER cached:
// stale safety state is worse than no state.

const CACHE = "mrrc-ft8-shell-v13";
const SHELL = [
  "/static/index.html",
  "/static/css/app.css",
  "/static/js/main.js",
  "/static/js/api.js",
  "/static/js/state.js",
  "/static/js/streams.js",
  "/static/js/waterfall.js",
  "/static/js/candidates.js",
  "/static/js/safety.js",
  "/static/js/toast.js",
  "/static/manifest.webmanifest",
  "/static/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
    return; // network only, no caching of radio/safety data
  }
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request)),
  );
});
