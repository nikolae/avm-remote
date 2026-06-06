// Minimal service worker: cache the app shell so "Add to Home Screen" launches
// instantly and works briefly offline. Live state always comes from the network
// (WebSocket / API), which we never cache.
const CACHE = "avm-remote-v1";
const SHELL = [
  "./",
  "index.html",
  "styles.css",
  "app.js",
  "manifest.webmanifest",
  "icons/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Never intercept API or WebSocket traffic.
  if (url.pathname.startsWith("/api") || url.pathname === "/ws") return;
  if (event.request.method !== "GET") return;

  // Network-first for the shell, falling back to cache when offline.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(event.request).then((r) => r || caches.match("index.html")))
  );
});
