const CACHE_NAME = "boatrace-v5";

// v5 の変更点:
//  - index.html / pdca.html / アイコンを追加。これらが無いとオフラインで
//    アプリ自体が開けず、PWA として成立していなかった
//  - addAll は1件でも失敗すると install 全体が失敗するため個別に登録する
//  - 画面遷移(navigate)がオフラインで失敗したとき index.html を返す
const STATIC_ASSETS = [
  "./",
  "index.html",
  "pdca.html",
  "css/style.css",
  "js/app.js",
  "manifest.json",
  "icons/icon-192.png",
  "icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((c) =>
      // 1件の失敗で全体を巻き込まないよう individually add する
      Promise.all(STATIC_ASSETS.map((u) => c.add(u).catch(() => null)))
    )
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
  const req = e.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // data/*.json は毎日更新されるためネットワーク優先。
  // 成功したらキャッシュも更新し、オフライン時は最後に取れた内容を返す。
  if (url.pathname.includes("/data/")) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // 画面遷移: オフラインなら index.html を返してアプリを起動できるようにする
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req).catch(() =>
        caches.match(req).then((r) => r || caches.match("index.html"))
      )
    );
    return;
  }

  // 静的アセットはキャッシュ優先
  e.respondWith(caches.match(req).then((cached) => cached || fetch(req)));
});
