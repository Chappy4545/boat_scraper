const CACHE_NAME = "boatrace-v17";

// v5: index.html / アイコンを追加。これらが無いとオフラインでアプリ自体が
//     開けず、PWA として成立していなかった。addAll は1件でも失敗すると
//     install 全体が落ちるため個別に登録する。navigate がオフラインで
//     失敗したときは index.html を返す。
// v6: pdca.html を収支タブへ統合したため一覧から削除
//     （存在しないファイルを登録しない）。
// v7: 取得停止を知らせる警告バナーを追加。旧 JS/CSS が残ると出ないので更新。
// v8: 検証モード（賭けずに記録だけ）の告知を追加。これが出ないと
//     「買うつもりの買い目」と区別がつかないので、確実に配る。
// v9: 検証中の候補ルールを買い目一覧から分離（件数だけ表示）。
// v10: 前夜のデイリーチェック結果を読んで、異常時だけ知らせる。
// v11: その警告に点検時刻を出す（いつ時点の話か分からなかった）。
// v12: 静的アセットを stale-while-revalidate に変更。
//      ⚠️ ここを上げ忘れると端末に更新が一切届かない。実際 v11(08-16)のまま
//      app.js を4回更新し、8/29 まで**2週間**古いままだった:
//        08-23 前日実績が候補ルールを数えていた修正
//        08-24 候補ルールを shrink_adj へ
//        08-25 候補ルールを top1_value へ
//        08-26 買い目タップ時の出走表・確率の修正
//      とくに候補ルールの除外が `rule === "market_blend"` のままで、
//      買っていない候補（賭け金0）が買い目として並んでいた（08-24〜28 で56行）。
// v13: 賭式を6つに拡張（単勝・複勝・拡連複・2連複・3連複・3連単）。
//      層と実測回収率のバッジを追加。賭け金0の行は金額を出さない。
//      以後は次回起動時に自動で新しくなるので、バージョンを上げ忘れても
//      1回ぶん遅れるだけで済む。それでも変更時は上げること。
// v14: 6賭式が画面に出ていなかったのを修正（賭け金0の行が一覧から
//      落ちていた）。賭式フィルターを追加。EV帯の実測値を確定オッズ基準
//      から締切前の板基準へ差し替え（高EVほど悪いので色も反転）。
//      → memory: project_pwa_display_split
// v15: 挙動の変更なし（コメントのみ）。isPurchased を is_final_pick で
//      絞りかけて取りやめた経緯を残した。買えなかった買い目は**書く側**で
//      防ぐ（main._closed_race_ids）。ここで絞ると、更新が止まった日の
//      記録まで消える。
// v16: EV の隣に「実質EV」を出す。表示EVは買う時点のオッズで計算して
//      おり、そのオッズは締切までに縮むので過大。実運用492本で割り戻した
//      （EV3.0以上は約1/3になる）。縮みはEVで選んだことの副作用で、
//      EVで選んでいない記録のみの買い目は縮み1.000だった。
// v17: 「買うべき」/「すべて」の切替。既定は買うべき（812件→85件）。
//      絞る基準は**モデルの確率**。EVで絞ると悪化するが、確率で絞ると
//      両窓とも改善した（17,090レース）。並び順の既定も確率にした。
//      複勝 p>=0.945 / 拡連複 p>=0.778 / 2連複 p>=0.435。
//      ⚠️ 賭け金が付いた買い目は条件に関係なく必ず出す。
const STATIC_ASSETS = [
  "./",
  "index.html",
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

  // 静的アセット: まずキャッシュを返し、裏で取り直して次回に備える
  // （stale-while-revalidate）。キャッシュ優先のみだと CACHE_NAME を上げない
  // 限り永久に古いままで、画面の修正がまったく届かない。表示の速さは保ちつつ、
  // 取り込み忘れても遅れは1回ぶんに収まる。
  e.respondWith(
    caches.match(req).then((cached) => {
      const fresh = fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || fresh;
    })
  );
});
