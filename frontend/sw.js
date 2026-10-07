// Service worker: makes the app installable and lets it open instantly / offline.
// App files: network first (so updates arrive), cached copy when offline.
// Live data (API, WebSocket, video, webhooks) always goes to the network.
const CACHE = "maseru-traffic-v1";
const SHELL = [
  "/", "/index.html", "/styles.css", "/app.js", "/manifest.json",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png", "/icons/favicon-32.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.origin === location.origin && /^\/(api|ws|webhooks)\b/.test(url.pathname)) return;
  // map tiles change rarely but are many: cache-first, keep whatever was viewed
  const cacheFirst = url.hostname.endsWith("tile.openstreetmap.org") || url.hostname.includes("cdn");
  e.respondWith(
    cacheFirst
      ? caches.match(e.request).then((hit) => hit || fetchAndStore(e.request))
      : fetchAndStore(e.request).catch(() => caches.match(e.request).then((hit) => hit || caches.match("/")))
  );
});

async function fetchAndStore(request) {
  const res = await fetch(request);
  if (res.ok || res.type === "opaque") {
    const copy = res.clone();
    caches.open(CACHE).then((c) => c.put(request, copy));
  }
  return res;
}
