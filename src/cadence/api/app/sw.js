// Offline worker for the app shell only.
//
// The shell (this page, its styles, script, font and icons) is cached so the
// app opens instantly and works without a connection. The API is never
// cached: a conversation is not something to serve stale. Bump CACHE when
// the shell changes so every phone picks up the new files on next open.

const CACHE = "cadence-app-v1";
const SHELL = [
  "/app/",
  "/app/index.html",
  "/app/app.css",
  "/app/app.js",
  "/app/manifest.webmanifest",
  "/app/icon.svg",
  "/app/icon-192.png",
  "/app/icon-512.png",
  "/app/atkinson-hyperlegible-400.woff2",
  "/app/atkinson-hyperlegible-700.woff2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isShell = event.request.method === "GET" && url.origin === self.location.origin && url.pathname.startsWith("/app/");
  if (!isShell) return; // the API goes straight to the network

  // Serve from the cache at once, then refresh the cached copy in the
  // background so the next open has the latest shell.
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(event.request);
      const refresh = fetch(event.request)
        .then((response) => {
          if (response.ok) cache.put(event.request, response.clone());
          return response;
        })
        .catch(() => cached);
      return cached || refresh;
    })
  );
});
