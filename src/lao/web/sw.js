// 画面の外枠だけを保存し、オフラインでも開けるようにする。
// /api と /files と /v1 は保存しない（会話や成果物を残さない）。
const CACHE = "lao-shell-v1";
const SHELL = ["/", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (/^\/(api|files|v1)\//.test(url.pathname)) return;
  if (!SHELL.includes(url.pathname)) return;
  // 新しい版を優先し、つながらないときだけ保存した版を返す
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(url.pathname, copy));
        return r;
      })
      .catch(() => caches.match(url.pathname))
  );
});
