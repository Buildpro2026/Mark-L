// JARVIS Command Center — minimal app-shell service worker.
// Cache-first for the static shell (HTML/JS/vendor), always network-first
// for /3d/api/* and /3d/ws so live data/nav is never served stale.
const CACHE_NAME = "jarvis-3d-shell-v1";
const SHELL_URLS = [
  "/3d",
  "/3d/assets/app.js",
  "/3d/assets/vendor/three.module.js",
  "/3d/assets/vendor/OrbitControls.js",
  "/3d/assets/manifest.json",
  "/3d/assets/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/3d/api/") || url.pathname.startsWith("/3d/ws")) {
    return; // never intercept live data/nav — let the network handle it
  }
  if (url.pathname.startsWith("/3d")) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        const network = fetch(event.request)
          .then((resp) => {
            if (resp && resp.ok) {
              caches.open(CACHE_NAME).then((cache) => cache.put(event.request, resp.clone()));
            }
            return resp;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});
