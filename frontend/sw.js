// PulseChain AI service worker. __BUILD__ is replaced by the server with a hash of the shipped frontend files,
// so installed copies update when the frontend changes.
//  - App shell: precached from /asset-manifest.json at install; served cache-first.
//  - /api GET: network-first; the last good response is cached and served (tagged) when offline.
//  - Writes are never handled here; the page queues eligible edits itself and replays them on reconnect.
const BUILD = "__BUILD__";
const SHELL = `pulsechain-shell-${BUILD}`;
const DATA = "pulsechain-data-v1";

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    let files = [];
    try { files = (await (await fetch("/asset-manifest.json", { cache: "no-store" })).json()).files; } catch { /* fall through */ }
    await cache.addAll(["/", ...files]);
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith("pulsechain-shell-") && k !== SHELL) await caches.delete(k);
    await self.clients.claim();
  })());
});

const offlineJson = (cached) => {
  const headers = new Headers(cached.headers);
  headers.set("X-PulseChain-Cache", "offline");
  return cached.blob().then((b) => new Response(b, { status: cached.status, statusText: cached.statusText, headers }));
};

self.addEventListener("fetch", (event) => {
  const req = event.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    event.respondWith((async () => {
      const cache = await caches.open(DATA);
      try {
        const res = await fetch(req);
        if (res.ok) await cache.put(req.url, res.clone());
        return res;
      } catch {
        const hit = await cache.match(req.url);
        if (hit) return offlineJson(hit);
        return new Response(JSON.stringify({ error: { code: "offline", message: "You are offline and this view has not been saved on this device yet." } }),
          { status: 503, headers: { "Content-Type": "application/json", "X-PulseChain-Cache": "offline" } });
      }
    })());
    return;
  }

  if (url.pathname === "/sw.js" || url.pathname === "/asset-manifest.json") return;

  event.respondWith((async () => {
    const cache = await caches.open(SHELL);
    const key = req.mode === "navigate" ? "/" : req;
    const hit = await cache.match(key, { ignoreSearch: true });
    if (hit) return hit;
    try {
      const res = await fetch(req);
      if (res.ok) cache.put(req, res.clone());
      return res;
    } catch {
      return req.mode === "navigate" ? (await cache.match("/")) || Response.error() : Response.error();
    }
  })());
});
