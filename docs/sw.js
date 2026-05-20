const CACHE_NAME = "boatrace-v3";
const STATIC_ASSETS = ["css/style.css", "js/app.js", "manifest.json"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((c) => c.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // data/*.json は毎日更新されるためネットワーク優先（オフライン時のみキャッシュ）
  if (url.pathname.includes("/data/")) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }

  // 静的アセットはキャッシュ優先
  e.respondWith(
    caches.match(e.request).then((cached) => cached || fetch(e.request))
  );
});
