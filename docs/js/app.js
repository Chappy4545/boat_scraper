"use strict";

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}

// ════════════════════════════════
// 状態
// ════════════════════════════════
const state = {
  page: "bets",
  date: todayStr(),
  filters: {
    bets:  { stadium: null, betType: null },
    races: { stadium: null, grade: null },
  },
  betsSort: "prob",     // "prob" | "ev" | "race"（買うべきは確率で選ぶので既定は確率）
  betsView: "buy",      // "buy" = 買うべきだけ / "all" = 総当たり812件
  betsShowAll: false,   // 買い目一覧の表示上限を外したか（6賭式で1日800本超）
  evInfoOpen: false,
  _racesCache: [],
  _betsCache:  [],      // 成績用（買った買い目だけ）
  _listCache:  [],      // 画面用（買う + 推奨のみ）
  _trialCache: [],      // 隠している検証行
  // 検証モード（賭けずに記録だけしている状態）。meta.json から入る。
  // 収支タブは買い目タブを開かずに見られるので、起動時に一度読んでおく。
  _paper: false,
};

fetch("data/meta.json", { cache: "no-store" })
  .then(r => (r.ok ? r.json() : null))
  .then(m => { if (m && m.paper_mode) state._paper = true; })
  .catch(() => {});

// ════════════════════════════════
// ユーティリティ
// ════════════════════════════════
function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}
function fmtDate(str) {
  const d = new Date(str + "T00:00:00");
  const days = ["日","月","火","水","木","金","土"];
  return `${d.getMonth()+1}/${d.getDate()}(${days[d.getDay()]})`;
}
function addDays(str, n) {
  const d = new Date(str + "T00:00:00");
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
}
async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}
function showToast(msg, ms = 2500) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), ms);
}

// ── EV カラー ──
// ⚠️ 2026-08-31 に反転させた。それまでは「EVが高いほど派手」で、
// EV3.0以上に「実測 回収率 239〜280%」と書いていた。
// あの数字は**確定オッズ（レース後にしか分からない値）**で測ったもので、
// 8/30 に締切前の板だけで測り直したら逆になった（1,032レース）:
//
//     全部買う      75.0%   （無作為 74.2%）
//     EV 1.0以上    71.6%
//     EV 1.5以上    63.4%
//     EV 2.0以上    54.1%   ← 画面には142〜174%と出ていた
//     EV 2.5以上    48.9%
//
// **絞るほど単調に悪化する。** 板の高オッズは妙味ではなく雑音で、締切までに
// 縮む。高EVを目立たせるのは「最も損な買い目を最も目立たせる」ことなので、
// 色の向きを実測に合わせる。→ memory: project_model_has_no_edge
function evColor(ev) {
  if (ev >= 2.5) return "#e57373";   // 実測 48.9%。最も悪い＝警告色
  if (ev >= 2.0) return "#ffb74d";   // 54.1%
  if (ev >= 1.5) return "#ffd54f";   // 63.4%
  return "#90a4ae";                   // 71.6%。相対的には最もまし
}

// 表示している EV は「買う時点のオッズ」で計算している。そのオッズは
// 締切までに縮むので、EV はそのぶん過大になっている。
//
// 2026-08-31 実測（実運用 492本・`scripts/odds_shrink.py`）:
// 買った時のオッズ ÷ 確定オッズ の中央値
//
//     EV 1.2-1.5   1.292      EV 2.0-3.0   1.986
//     EV 1.5-2.0   1.588      EV 3.0以上   3.333
//     記録のみ     1.000  ← EVで選んでいないので縮まない
//
// **記録のみが 1.000** なのが決定的。縮みは市場の性質ではなく
// 「EVが高い＝オッズが上振れしている組合せを選んでいる」ことの副作用
// （optimizer's curse）。前半/後半の2窓で同じ関係が出た（EV3.0以上だけ
// 本数不足で不一致）。→ memory: project_odds_board_vs_final
const EV_SHRINK = [[3.0, 3.333], [2.0, 1.986], [1.5, 1.588], [0, 1.292]];

// ⚠️ 縮みは「EVの値」の性質ではなく「**EVで選んだこと**」の性質。
// EV で選んでいない買い目（rule="record" = 賭式ごとに確率最大の1点）は
// 実測で縮み 1.000 だった。ここに係数をかけると、縮まない買い目まで
// 割り引いて表示することになる。2026-08-31 に一度やりかけた。
function realEv(ev, bet) {
  if (bet && bet.rule === RECORD_RULE) return ev;   // EVで選んでいない
  for (const [lo, f] of EV_SHRINK) if (ev >= lo) return ev / f;
  return ev;
}

// ── バッジ生成 ──
function gradeBadge(grade) {
  if (!grade || grade === "一般") return "";   // 一般は非表示（ノイズ削減）
  const map = {
    SG: "grade-sg", PGI: "grade-pgi",
    G1: "grade-g1", G2: "grade-g2", G3: "grade-g3",
  };
  const cls = map[grade] ?? "grade-gen";
  return `<span class="badge ${cls}">${grade}</span>`;
}

function categoryBadges(raceType, isNight) {
  let html = "";
  if (isNight) html += `<span class="badge badge-night">🌙 ナイター</span>`;
  if (raceType && raceType.includes("レディース")) {
    html += `<span class="badge badge-ladies">♀ レディース</span>`;
  }
  return html;
}

function betTypeLabel(t) {
  return { tansho:"単勝", fukusho:"複勝", kakurenfuku:"拡連複",
           nirentan:"2連単", nirenfuku:"2連複",
           sanrenfuku:"3連複", sanrentan:"3連単" }[t] ?? t;
}

// 賭式ごとの層と、未見データでの実測回収率。
//
// 2026-08-30、17,090レース・独立2窓で測った「モデルの確率が最大の1点」の成績。
// 表示は保守側（悪い方の窓）を出す。⚠️ どれも100%未満なので、
// 買えば平均して減る。数字を出さずに「買い目」とだけ書くと誤解を招く。
// ⚠️ buy は configs/config.yaml の betting.bet_types と一致していること。
// リテラルのまま書く（tests/test_buy_config.py が正規表現で読んで見張る）。
// 2026-09-03: 固い4賭式を実運用に。3連複・3連単はモデルが確立するまでペーパー。
const BET_TIER = {
  fukusho:     { tier: "固い", roi: 93.5, buy: true },
  tansho:      { tier: "固い", roi: 90.8, buy: true },
  kakurenfuku: { tier: "固い", roi: 84.8, buy: true },
  nirenfuku:   { tier: "勝負", roi: 82.4, buy: true },
  sanrenfuku:  { tier: "勝負", roi: 80.1, buy: false },
  sanrentan:   { tier: "夢",   roi: 79.4, buy: false },
};
const BET_ORDER = ["fukusho", "tansho", "kakurenfuku",
                   "nirenfuku", "sanrenfuku", "sanrentan"];
// ── 「買うべき」の絞り込み ──
//
// 画面は 144レース × 6賭式 = 812件の総当たりを出していた。推奨ではなく一覧で、
// この中から買うものを選べない。**何で絞るか**を 2026-08-31 に測った
// （`scripts/prob_filter.py`、17,090レース・独立2窓・オッズを使わない）。
//
// ⚠️ EV で絞ってはいけない。EV>=2.0 の回収率は 54.1% で、**絞るほど悪化**する
// （2026-08-30 実測）。EV はオッズを含むので、オッズが上振れした組合せを
// 選んでしまう。
//
// **モデルの確率で絞ると逆に良くなる。** 確率はオッズを一切見ないので、
// その選択バイアスが乗らない。両窓とも「全部買う」を上回った条件だけ採用:
//
//     賭式      全部買う(A/B)   採用した条件        実測(A/B)
//     複勝      95.5/93.5     p>=0.945        96.6/96.7   ⭐最良
//     拡連複    85.9/84.8     p>=0.778        91.0/88.5
//     2連複     85.3/82.4     p>=0.435        90.7/84.6
//     単勝      92.1/90.8     ―（窓Aで下回る）
//     3連複     80.7/80.1     ―（最上位帯が4番目に劣る）
//     3連単     78.5/79.4     ―（窓A100.1%だが窓B76.9%＝雑音）
//
// ⚠️ **どれも 100% 未満。** 「買うべき」は「最も損が小さい」の意味であって、
// 勝てるという意味ではない。画面にも必ず実測値を併記すること。
// ⚠️ 2026-08-31 夜、**一度も使っていない 2〜4月（10,809レース）で確かめた**
// （`scripts/prob_filter_confirm.py`、仮説と判定基準は結果を見る前に確定）。
// 結果は**部分的な再現**にとどまった:
//
//   賭式     全部買う  絞った   差      95%区間        窓ごとの符号  判定
//   複勝      94.4%   95.6%  +1.2pt [-1.0〜+3.4]  −−++      × 再現せず
//   拡連複    85.2%   88.6%  +3.4pt [+0.1〜+6.7]  −+++      ○ 再現
//   2連複     84.9%   86.3%  +1.4pt [-3.9〜+6.7]  −+−+      × 再現せず
//
// 向きは3つとも正だが、探索の窓（+3.2〜+5.4pt）の半分ほどに縮み、
// 区間が0を含む。**確かめられたのは拡連複だけ**で、それも下限 +0.1pt。
// 3件を検定しているので、1件だけの通過は偶然と区別しづらい。
//
// それでも画面には残す。向きは正で、少なくとも害は無く、812件の総当たりを
// そのまま並べるより選べるため。ただし**確かめられたかどうかを表示する。**
// ── 確度（どれが堅いか）──
//
// ⭐ **的中率は精度よく測れる。回収率は測れない。**
// 同じ17,000レースでも、回収率の95%区間は±5〜15pt になるのに対し、
// 的中率は**±1〜2pt** に収まる（配当のばらつきが効かないため）。
// 「勝てるか」は言えなくても「**当たりやすいか**」ははっきり言える。
//
// 賭式ごとに確率の四分位で4段階に分け、未使用データ（2026-02-02〜04-22・
// 10,809レース・探索に一度も使っていない）で的中率を測った。
// `scripts/confidence_bands.py`
//
//   複勝  S(p>=0.904) 的中88.9% / A 80.6% / B 72.2% / C 57.4%
//   単勝  S(p>=0.650) 的中76.0% / A 64.3% / B 51.4% / C 35.5%
//   拡連複 S(p>=0.729) 的中67.7% / A 58.2% / B 51.1% / C 43.2%
//   2連複 S(p>=0.387) 的中43.0% / A 34.1% / B 27.6% / C 20.5%
//   3連複 S(p>=0.354) 的中34.9% / A 26.4% / B 21.5% / C 15.2%
//   3連単 S(p>=0.117) 的中15.3% / A 10.4% / B  8.2% / C  3.8%
//
// **6賭式すべてで単調**、しかも探索データの値とほぼ一致した
// （複勝S 88.9% vs 87.7%、2連複S 43.0% vs 43.3%）。期間に依らない。
//
// ⚠️ **的中率が高い＝勝てる、ではない。** 当たりやすいぶん配当は低い。
// S帯の回収率は 86〜97% で、どれも100%未満。必ず分けて書くこと。
const CONFIDENCE = {
  fukusho:     { cuts: [0.674, 0.802, 0.904], hit: [0.574, 0.722, 0.806, 0.889] },
  tansho:      { cuts: [0.381, 0.504, 0.650], hit: [0.355, 0.514, 0.643, 0.759] },
  kakurenfuku: { cuts: [0.563, 0.645, 0.729], hit: [0.431, 0.511, 0.582, 0.677] },
  nirenfuku:   { cuts: [0.255, 0.316, 0.387], hit: [0.205, 0.277, 0.341, 0.430] },
  sanrenfuku:  { cuts: [0.227, 0.285, 0.354], hit: [0.152, 0.215, 0.264, 0.349] },
  sanrentan:   { cuts: [0.061, 0.086, 0.117], hit: [0.038, 0.082, 0.104, 0.153] },
};
// ⚠️⚠️ 段階は**その賭式の中での帯**。賭式をまたぐ絶対基準にしてはいけない。
//
// 2026-08-31〜09-03 まで「的中率70%以上ならS」という絶対基準だった。
// 各賭式の的中率の上限はこうなっている:
//
//     複勝 88.9% / 単勝 75.9% / 拡連複 67.7% /
//     2連複 43.0% / 3連複 34.9% / 3連単 15.3%
//
// つまり **拡連複・3連複・3連単は永久にSにならず、既定の一覧から丸ごと
// 消えていた**（9/3 実測: 971件中180件しか出ておらず、その内訳は
// 複勝113 + 単勝35 + 買った2連複32。他3賭式は0件）。
// 上限15%〜89%の賭式に同じ物差しを当てれば当然そうなる。
//
// 段階の意味を「その賭式の中で上位か」に変えれば、どの賭式にもSが出る。
// 「3連単のSは的中15%」という事実は **hit を絶対値で併記して**伝える。
// ⚠️ hit の併記は「S＝勝てる」と読ませない唯一の歯止め。外さないこと。
//
// （なお四分位で切ること自体は 2026-08-31 に一度やって戻した経緯がある。
//  当時の問題は「常に全体の25%がSで何も選んでいない」ことだったが、
//  それは**1本のリストに全賭式を混ぜていたから**で、賭式ごとに
//  見出しを分けて出すいまの形なら「その賭式の上位25%」で意味が通る。）
const GRADE_BY_BAND = ["C", "B", "A", "S"];

// その買い目の確度。段階（賭式内の帯）と、その帯の**実測**的中率を返す。
function confidenceOf(bet) {
  const c = bet && CONFIDENCE[bet.bet_type];
  if (!c) return null;
  const p = bet.model_prob || 0;
  let i = 0;
  while (i < c.cuts.length && p >= c.cuts[i]) i++;
  const band = ["下位25%", "中位", "上位25%", "最上位25%"][i];
  return {
    grade: GRADE_BY_BAND[i],
    band: i,
    top: i === c.cuts.length,          // その賭式の最上位帯か
    hit: c.hit[i],
    label: `${betTypeLabel(bet.bet_type)}の中で${band}`,
  };
}

// 既定の一覧に出すか＝その賭式の最上位帯か。
// ⚠️ 実際に賭け金が付いている買い目は、確度に関係なく必ず出す。
// 絞り込みで自分が買った買い目が消えるのが一番まずい。
function isRecommended(bet) {
  if (!bet) return false;
  if (isPurchased(bet)) return true;
  const c = confidenceOf(bet);
  return !!c && c.top;               // S = その賭式の最上位帯
}

function tierBadge(t) {
  const v = BET_TIER[t];
  if (!v) return "";
  return `<span class="tier-badge" title="未見17,090レースでの実測回収率">`
       + `${v.tier} ${v.roi.toFixed(0)}%</span>`;
}

// 確度の印。**実測の的中率をそのまま出す**のが肝心で、段階の記号だけだと
// 「S=勝てる」と読まれてしまう。的中率は未使用データで測った値。
// ⚠️ 段階は**その賭式の中での順位**。3連単のSと複勝のSは的中率が
// 15%と89%で全然違う。だから記号の隣に必ず実測値を出す。
function confBadge(bet) {
  const c = confidenceOf(bet);
  if (!c) return "";
  return `<span class="conf conf--${c.grade}" title="確度 ${c.grade}（${c.label}）：`
       + `この帯の実測的中率（未使用データ10,809レース・誤差±1〜2pt）。`
       + `⚠️ 段階は賭式の中での順位です。賭式をまたぐと意味が違います`
       + `（3連単のSは的中15%、複勝のSは89%）。`
       + `当たりやすいぶん配当は低く、回収率はどれも100%未満です">`
       + `${c.grade} <b>的中${(c.hit * 100).toFixed(0)}%</b></span>`;
}

// actual_payout は「100円あたりの払戻額」（オッズ3.6倍 → 360）。
// 実際の払戻は 賭け金 × payout / 100。
// 以前はこの値をそのまま金額として表示・集計しており、
// 回収額が実際の 1/5 程度に見えていた（賭け金500円なら5倍の誤差）。
function payoutOf(bet) {
  if (!bet || bet.actual_payout == null) return 0;
  return Math.round((bet.recommended_amount || 0) * bet.actual_payout / 100);
}
// ── 買い目の3分類 ──
// ⚠️ 「成績に入れるか」と「画面に出すか」は別の問い。
// 2026-08-31 まではこれを1つの関数(isCandidate)で兼ねていた。そのため
// 8/30 に6賭式へ広げたとき、賭け金0で記録している単勝・複勝・拡連複・
// 3連複・3連単が **画面からも消え**、2連複しか見えなくなった。
// 787本を生成しながら1本も表示していない状態が丸一日続いた。
//
//   買う      賭け金>0 かつ検証ルールでない。成績に入り、画面にも出る
//   推奨のみ  rule="record"。6賭式の推奨。画面には出すが成績には入れない
//   検証行    market_blend / shrink_adj / top1_value。同じレース・同じ賭式に
//             別ルールで重複する行なので、画面にも成績にも出さない
//
// 成績から外す理由（2026-08-23）: 判定だけはされるので is_hit が入り、
// 素通しにすると「買っていない買い目」が的中率の分母に乗る。前日カードだけ
// この除外が抜けており、前日実績 5/44 と日別ページ 5/33 が食い違っていた。
// ⚠️ CANDIDATE_RULES は main.py の同名定数と一致していること。
// リテラルの配列のまま書く（tests/test_pwa_cache_version.py が正規表現で
// 読み、main.py とズレていないかを見張っている）。
const CANDIDATE_RULES = ["market_blend", "shrink_adj", "top1_value", "record"];
const RECORD_RULE = "record";
const TRIAL_RULES = CANDIDATE_RULES.filter(r => r !== RECORD_RULE);
const TRIAL_RULE_LABEL = {
  market_blend: "市場7:モデル3の混合（2026-08-24 棄却）",
  shrink_adj:   "オッズの縮み補正",
  top1_value:   "確率最大の1点 × EV2.0",
};

// 成績（損益・回収率・的中率）に入れてよい買い目か。
// 賭け金が付いていても検証ルールなら入れない（8/30 に record へ 500円が
// 付くバグがあり、二重の防波堤として rule も見る）。
//
// ⚠️ **is_final_pick で絞ってはいけない。** 2026-08-31 に一度そうしかけた。
// 「確定しなかった買い目＝締切に間に合わず買えなかった買い目」と考えたが、
// 実データを見ると 24本中10本は午後・夜のレースだった（8/26 の8本は
// 13:16 に買い目生成が止まった日のもの）。**朝から画面に出ていて買えた**
// 買い目で、更新が止まったせいで確定しなかっただけ。除外すると
// 都合の悪い日の記録が丸ごと消える。
//
// 「買えなかった買い目」は**書く側**で防ぐ。締切を過ぎたレースには
// 賭け金を付けない（main._closed_race_ids）。
// → memory: project_unbuyable_bets_in_roi
function isPurchased(bet) {
  return !!bet && (bet.recommended_amount || 0) > 0
    && !CANDIDATE_RULES.includes(bet.rule);
}
// 買い目一覧に出してよい買い目か。検証行だけを隠す。
function isDisplayable(bet) {
  return !!bet && !TRIAL_RULES.includes(bet.rule);
}
function bn(no) {
  return `<span class="bn bn-${no}">${no}</span>`;
}
function comboSpans(combination) {
  return combination.split("-").map(n => bn(parseInt(n))).join(
    '<span style="color:var(--muted);margin:0 1px">-</span>'
  );
}

// ── フィルターバー ──
// order を渡すとその順に並べる（渡さなければ従来どおり名前順）。
// 賭式は「固い→夢」の意味のある並びがあり、名前順にすると層が読めない。
function buildFilterBar(items, getKey, activeVal, onSelect, allLabel = "すべて", order = null) {
  const counts = {};
  items.forEach(item => {
    const k = getKey(item) || "—";
    counts[k] = (counts[k] || 0) + 1;
  });
  const keys = order
    ? order.filter(k => counts[k])          // 出ていない賭式のチップは出さない
    : Object.keys(counts).sort();

  const chips = [`<button class="filter-chip${activeVal === null ? " active" : ""}" data-val="">
    ${allLabel} <span class="filter-chip__count">${items.length}</span>
  </button>`];
  keys.forEach(k => {
    const isActive = activeVal === k;
    chips.push(`<button class="filter-chip${isActive ? " active" : ""}" data-val="${k}">
      ${k} <span class="filter-chip__count">${counts[k]}</span>
    </button>`);
  });

  const bar = document.createElement("div");
  bar.className = "filter-bar";
  bar.innerHTML = chips.join("");
  bar.querySelectorAll(".filter-chip").forEach(btn => {
    btn.addEventListener("click", () => onSelect(btn.dataset.val || null));
  });
  return bar;
}

// ════════════════════════════════
// ナビゲーション
// ════════════════════════════════
function navigate(page) {
  state.page = page;
  document.querySelectorAll(".snav-btn, .bnav-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.page === page);
  });
  document.querySelectorAll(".page").forEach(p => {
    p.classList.toggle("active", p.id === `page-${page}`);
  });
  loadPage(page);
}

document.querySelectorAll(".snav-btn, .bnav-btn").forEach(btn => {
  btn.addEventListener("click", () => navigate(btn.dataset.page));
});

// ════════════════════════════════
// 日付ナビ
// ════════════════════════════════
function updateDateLabel() {
  document.getElementById("current-date").textContent = fmtDate(state.date);
}
document.getElementById("prev-date").addEventListener("click", () => {
  state.date = addDays(state.date, -1);
  state.filters.bets.stadium = null;
  state.filters.races.stadium = null;
  state.filters.races.grade = null;
  updateDateLabel();
  loadPage(state.page);
});
document.getElementById("next-date").addEventListener("click", () => {
  if (state.date >= todayStr()) { showToast("未来の日付には進めません"); return; }
  state.date = addDays(state.date, 1);
  state.filters.bets.stadium = null;
  state.filters.races.stadium = null;
  state.filters.races.grade = null;
  updateDateLabel();
  loadPage(state.page);
});

// ════════════════════════════════
// モーダル
// ════════════════════════════════
document.querySelector(".modal__backdrop").addEventListener("click", closeModal);
document.querySelector(".modal__close").addEventListener("click", closeModal);
function openModal(html) {
  document.getElementById("modal-body").innerHTML = html;
  document.getElementById("modal").classList.remove("hidden");
}
function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}

// ════════════════════════════════
// 買い目ページ
// ════════════════════════════════
async function loadBets() {
  const page = document.getElementById("page-bets");
  let container = document.getElementById("bet-list");

  // ローディング
  container.innerHTML = '<div class="empty">読込中…</div>';

  state._betsCache = [];
  state._racesCache = [];
  const isToday = state.date === todayStr();
  const yDate   = addDays(state.date, -1);
  try {
    const [bets, races, yBets, meta, health] = await Promise.all([
      // 404 を投げさせない。ファイルが無いこと自体が知りたい情報で、
      // 「買い目ゼロの日」と「取得が止まった日」は区別しないといけない。
      api(`data/bets_${state.date}.json`).catch(e => {
        if (e.message === "404") return null;
        throw e;
      }),
      api(`data/races_${state.date}.json`).catch(() => []),
      isToday ? api(`data/bets_${yDate}.json`).catch(() => []) : Promise.resolve([]),
      // 検証モードかどうかは日付に関係なく要るので、meta は常に読む
      api(`data/meta.json`).catch(() => null),
      // 前夜のデイリーチェック。異常があったときだけ知らせる。
      api(`data/health.json`).catch(() => null),
    ]);
    // 3分類（isPurchased / isDisplayable の説明を参照）。
    // _betsCache = 成績用（買った買い目だけ）。day-panel と前日カードが使う。
    // _listCache = 画面用（買う + 推奨のみ）。買い目一覧が使う。
    // _trialCache = 隠している検証行。件数だけバナーで知らせる。
    const all = bets || [];
    state._betsCache  = all.filter(isPurchased);
    state._listCache  = all.filter(isDisplayable);
    state._trialCache = all.filter(b => TRIAL_RULES.includes(b.rule));
    state._racesCache = races;
    state._paper = !!(meta && meta.paper_mode);
    state._health = health;
    renderHealthBanner(isToday, bets, races, meta);

    // 更新時刻は独立行にせず day-panel の中へ（スマホの縦を1行ぶん節約）
    let refreshText = "";
    if (meta && meta.last_refreshed) {
      const t = new Date(meta.last_refreshed);
      const hm = t.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
      const src = meta.source === "github_actions" ? "自動更新" : "手動更新";
      refreshText = `オッズ最終更新 ${hm}（${src}）`;
    }
    document.getElementById("odds-refresh-time").textContent = "";

    renderDayPanel(state.date, state._betsCache, refreshText);
    renderYesterdayResult(yDate, yBets);

    // 一覧に出すものが1本も無いときだけ「買い目なし」。買う買い目が0でも
    // 推奨（賭け金0）が出ていれば見せる。ここを _betsCache で見ていたため、
    // 8/30 は推奨787本を持ちながら「推奨買い目はありません」を出しかねなかった。
    if (!state._listCache.length) {
      document.getElementById("bets-filter-area").innerHTML = "";
      document.getElementById("bets-summary").innerHTML = "";
      // レースはあるのに買い目が無い＝条件を満たさなかった日（正常）。
      // レースごと無い＝そもそも取得できていない（異常、上のバナーが出る）。
      container.innerHTML = races.length
        ? '<div class="empty">この日の推奨買い目はありません</div>'
        : '<div class="empty">この日のレースデータがありません</div>';
      return;
    }

    renderBets();
  } catch (e) {
    document.getElementById("bets-filter-area").innerHTML = "";
    document.getElementById("bets-summary").innerHTML = "";
    document.getElementById("day-panel").innerHTML = "";
    document.getElementById("yesterday-result").innerHTML = "";
    document.getElementById("health-banner").innerHTML = "";
    container.innerHTML = e.message === "404"
      ? '<div class="empty">この日のデータがありません</div>'
      : `<div class="empty">取得失敗 (${e.message})</div>`;
  }
}

// ── 取得が止まっていないかを画面で知らせる ──
// 2026-08-13、朝の更新が動かなかった日に画面へ出たのは「データがありません」
// だけだった。買い目ゼロの日と見分けがつかず、半日気づけなかった。
// 異常なときだけ出す（正常な日は何も出さない）。
function renderHealthBanner(isToday, bets, races, meta) {
  const el = document.getElementById("health-banner");
  if (!el) return;
  const parts = [];

  // 検証モードは日付に関係なく常に出す。買い目の見た目は今までと同じなので、
  // これが無いと「買うつもりの買い目」と区別がつかない。
  // デイリーチェックで引っかかった項目。異常時だけ出す。
  // いつ点検した結果かを必ず書く。夜の判定だけでなく手で回すこともあり、
  // 「昨夜の点検」と決め打ちすると、収集途中の記録を夜の結果として
  // 見せてしまう（2026-08-16 に発生）。
  const h = state._health;
  if (h && h.ng && h.ng.length) {
    const t = h.checked_at
      ? new Date(h.checked_at).toLocaleString("ja-JP",
          { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })
      : h.date;
    parts.push(healthAlert(
      `点検で ${h.ng.length} 件の異常（${t} 時点）`,
      `${h.date} 分の ${h.ng.join(" / ")} が想定どおりに動いていません。` +
      `その後に解消している場合もあります。`
    ));
  }

  if (meta && meta.paper_mode) parts.push(paperNotice());

  // 検証中のルールが何本出ているか。これらは同じレース・同じ賭式に別ルールで
  // 重複する行なので一覧には載せていない。件数だけ知らせないと、動いているのか
  // 止まっているのかが分からない。
  // ⚠️ ここは以前「市場7:モデル3の混合ルール」と決め打ちで書いていたが、
  // その混合ルールは 2026-08-24 に棄却済みで、実際に出ているのは別ルール
  // だった。ルール名は必ずデータから作る。
  const trial = state._trialCache || [];
  if (trial.length) {
    const names = [...new Set(trial.map(b => b.rule))]
      .map(r => TRIAL_RULE_LABEL[r] || r).join(" / ");
    parts.push(`<div class="notice notice--quiet" role="note">
      <span class="notice__icon" aria-hidden="true">検証</span>
      <div><strong>検証中のルールが ${trial.length} 本</strong>
      <div class="notice__body">${names}。買う買い目と重複するため一覧には
      載せていません（賭け金0で記録のみ）。</div></div>
    </div>`);
  }

  if (isToday) {
    const now  = new Date();
    const hour = now.getHours() + now.getMinutes() / 60;
    const hm   = now.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });

    if (!races || !races.length) {
      // 今日のレースが1件も無い = 朝の更新が走っていない。
      // 早朝はまだ取得していなくて当然なので 8 時以降だけ言う。
      if (hour >= 8) {
        parts.push(healthAlert(
          "今日のデータがまだありません",
          `朝の更新が完了していません（${hm} 時点）。PC の電源とネットワークを確認してください。`
        ));
      }
    } else if (meta && meta.last_refreshed && hour >= 10 && hour <= 21.5) {
      // レースはあるのにオッズ更新が止まっている。
      // 開催時間帯は 15 分ごとに更新されるので、1 時間空いたら異常。
      const mins = Math.round((now - new Date(meta.last_refreshed)) / 60000);
      if (mins >= 60) {
        parts.push(healthAlert(
          "オッズの更新が止まっています",
          `最終更新から ${mins} 分経過しています。表示中のオッズと期待値は古い可能性があります。`
        ));
      }
    }
  }
  el.innerHTML = parts.join("");
}

// 色だけに頼らず、記号と語で状態を示す
function healthAlert(title, body) {
  return `<div class="alert" role="status">
    <span class="alert__icon" aria-hidden="true">!</span>
    <div><strong>${title}</strong><div class="alert__body">${body}</div></div>
  </div>`;
}

// 検証モードの告知。異常ではないので警告色は使わず、常設の断り書きにする。
function paperNotice() {
  return `<div class="notice" role="note">
    <span class="notice__icon" aria-hidden="true">検証</span>
    <div><strong>この買い目は賭けていません</strong>
    <div class="notice__body">モデルは市場より正確でないと確認できたため、実際の購入は止めています。
    記録と判定は続いているので、表示中の損益は「賭けていたらこうなった」という仮の数字です。</div></div>
  </div>`;
}

// ── その日の状況パネル ──
// 実運用を始めた以上「今日いくら賭けて、いくら戻ったか」が最初に要る。
// 数値の形は stat tile の作法に合わせる:
//   ヒーロー数値は1画面に1つ(=損益) / 差分は符号つき / 状態は色だけに頼らず記号と語を添える
function renderDayPanel(dateStr, bets, refreshText = "") {
  const el = document.getElementById("day-panel");
  if (!bets || !bets.length) { el.innerHTML = ""; return; }
  // 更新時刻は独立した行にすると縦を1行ぶん食うので、パネルの中に入れる
  const sub = refreshText ? `<div class="day-panel__sub">${refreshText}</div>` : "";

  const settled  = bets.filter(b => b.is_hit === true || b.is_hit === false);
  const pending  = bets.length - settled.length;
  const hits     = settled.filter(b => b.is_hit === true);
  const invested = bets.reduce((s, b) => s + (b.recommended_amount || 0), 0);
  const settledInv = settled.reduce((s, b) => s + (b.recommended_amount || 0), 0);
  const returned = hits.reduce((s, b) => s + payoutOf(b), 0);
  const profit   = returned - settledInv;
  const roi      = settledInv > 0 ? returned / settledInv : null;
  const hitRate  = settled.length ? hits.length / settled.length : null;

  const yen = n => "¥" + Math.round(n).toLocaleString();
  const isToday = dateStr === todayStr();

  // 結果がまだ無い（＝これから）
  if (!settled.length) {
    el.innerHTML = `
      <section class="day-panel day-panel--pending">
        <div class="day-panel__head">
          <span class="day-panel__title">${isToday ? "今日" : fmtDate(dateStr)}の予定</span>
          <span class="chip chip--wait">結果待ち</span>
        </div>
        <div class="day-hero">
          <div class="day-hero__value">${yen(invested)}</div>
          <div class="day-hero__label">投資予定額</div>
        </div>
        <div class="inline-stats">
          <span><b>${bets.length}</b> 買い目</span>
          <span><b>${new Set(bets.map(b => b.race_id)).size}</b> レース</span>
          <span><b>${new Set(bets.map(b => b.stadium_name)).size}</b> 場</span>
        </div>
        ${sub}
      </section>`;
    return;
  }

  const good = profit >= 0;
  el.innerHTML = `
    <section class="day-panel ${good ? "day-panel--good" : "day-panel--bad"}">
      <div class="day-panel__head">
        <span class="day-panel__title">${isToday ? "今日" : fmtDate(dateStr)}の結果</span>
        ${pending ? `<span class="chip chip--wait">未確定 ${pending}件</span>` : `<span class="chip">確定</span>`}
      </div>
      <div class="day-hero">
        <div class="day-hero__value ${good ? "is-good" : "is-bad"}">
          ${good ? "+" : "−"}${yen(Math.abs(profit))}
        </div>
        <div class="day-hero__label">${state._paper ? "仮の損益（賭けていません）"
          : good ? "▲ 損益（プラス）" : "▼ 損益（マイナス）"}</div>
      </div>
      <div class="stat-row">
        <div class="stat">
          <div class="stat__val">${roi === null ? "—" : (roi * 100).toFixed(0) + "%"}</div>
          <div class="stat__lab">回収率</div>
        </div>
        <div class="stat">
          <div class="stat__val">${hits.length}<span class="stat__sub">/${settled.length}</span></div>
          <div class="stat__lab">的中</div>
        </div>
        <div class="stat">
          <div class="stat__val">${hitRate === null ? "—" : (hitRate * 100).toFixed(0) + "%"}</div>
          <div class="stat__lab">的中率</div>
        </div>
        <div class="stat">
          <div class="stat__val">${yen(settledInv)}</div>
          <div class="stat__lab">投資（確定分）</div>
        </div>
      </div>
      ${sub}
    </section>`;
}

function renderYesterdayResult(yDate, allBets) {
  const el = document.getElementById("yesterday-result");
  // 買った買い目だけを見る。推奨のみ・検証行は賭け金0で記録しているだけなので、
  // 混ぜると的中率の分母が膨らむ（日別ページとの食い違いの原因だった）。
  const bets = (allBets || []).filter(isPurchased);
  const settled = bets.filter(b => b.is_hit !== null && b.is_hit !== undefined);
  if (!settled.length) { el.innerHTML = ""; return; }

  const hits     = settled.filter(b => b.is_hit === true);
  const invested = settled.reduce((s, b) => s + (b.recommended_amount || 0), 0);
  const returned = hits.reduce((s, b) => s + payoutOf(b), 0);
  const roi      = invested > 0 ? returned / invested : 0;
  const profit   = returned - invested;   // 損益。回収額だけでは増減が分からない
  const roiCls   = roi >= 1 ? "val-good" : "val-bad";
  const hitRate  = settled.length > 0 ? hits.length / settled.length : 0;

  const BET_LABEL = { sanrentan:"3連単", sanrenfuku:"3連複", nirentan:"2連単", nirenfuku:"2連複" };

  // 的中一覧は出さない。8/10 は44件あり、買い目が画面外へ押し出されていた。
  // 詳細はカードをタップしてその日へ移動すれば見られる。
  // 的中一覧は買い目ページには出さない。8/10 は44件あり、買い目が画面外へ
  // 押し出されていた。日付をタップしてその日へ移動すれば全件見られる。
  const hitsHtml = "";

  el.innerHTML = `
    <div class="yesterday-card" id="yesterday-card-click">
      <div class="yesterday-card__head">
        <span class="yesterday-card__label">前日実績 <span class="yesterday-card__date">${fmtDate(yDate)}</span></span>
        <span class="yesterday-card__link">詳細 ›</span>
      </div>
      <div class="yesterday-card__stats">
        <div class="yesterday-stat">
          <div class="yesterday-stat__val ${roiCls}">${profit >= 0 ? "+" : "−"}¥${Math.abs(profit).toLocaleString()}</div>
          <div class="yesterday-stat__label">${profit >= 0 ? "▲ 損益" : "▼ 損益"}</div>
        </div>
        <div class="yesterday-stat">
          <div class="yesterday-stat__val ${roiCls}">${(roi * 100).toFixed(0)}<span class="yesterday-stat__denom">%</span></div>
          <div class="yesterday-stat__label">回収率</div>
        </div>
        <div class="yesterday-stat">
          <div class="yesterday-stat__val">${hits.length}<span class="yesterday-stat__denom">/${settled.length}</span></div>
          <div class="yesterday-stat__label">的中 ${(hitRate * 100).toFixed(0)}%</div>
        </div>
        <div class="yesterday-stat">
          <div class="yesterday-stat__val">¥${invested.toLocaleString()}</div>
          <div class="yesterday-stat__label">投資</div>
        </div>
      </div>
      ${hitsHtml}
    </div>`;

  document.getElementById("yesterday-card-click").addEventListener("click", () => {
    state.date = yDate;
    state.filters.bets.stadium = null;
    state.filters.races.stadium = null;
    state.filters.races.grade = null;
    updateDateLabel();
    loadPage(state.page);
  });
}

// EV説明パネルのHTML
const EV_EXPLAIN_HTML = `
<div class="ev-info-panel">
  <div class="ev-info-row">
    <span class="ev-info-formula">EV = モデル確率 × オッズ</span>
  </div>
  <p class="ev-info-desc">モデルが「当たりやすい」と判断した組み合わせのオッズが高いほどEVが上がります。理屈では EV&gt;1.0 で期待値プラスですが、<strong>実測はそうなっていません。</strong></p>
  <p class="ev-info-desc">下は 2026-08-30 に <strong>締切前の板だけ</strong>で測り直した回収率です（1,032レース）。EVで絞るほど悪くなります。以前この欄に出していた「EV3.0以上→239〜280%」は、レース後にしか分からない<strong>確定オッズ</strong>で測った数字で、実際には買えません。</p>
  <div class="ev-info-tiers">
    <span class="ev-tier" style="color:#90a4ae">1.0以上　実測 71.6%</span>
    <span class="ev-tier" style="color:#ffd54f">1.5以上　実測 63.4%</span>
    <span class="ev-tier" style="color:#ffb74d">2.0以上　実測 54.1%</span>
    <span class="ev-tier" style="color:#e57373">2.5以上　実測 48.9%</span>
  </div>
  <p class="ev-info-desc">参考: 何も選ばず全部買うと 75.0%、無作為に買うと 74.2%。<strong>まだ損益分岐(100%)を超える買い方は見つかっていません。</strong></p>
  <p class="ev-info-desc"><strong>「実質」とは</strong> — 表示中のEVは買う時点のオッズで計算しています。そのオッズは締切までに縮むので、EVはそのぶん過大です。実運用492本の実測（買った時のオッズ ÷ 確定オッズ）で割り戻したのが「実質」です。EVが高いほど縮みは大きく、EV3.0以上では約1/3になります。</p>
  <p class="ev-info-desc">縮むのは市場の性質ではなく<strong>EVで選んだことの副作用</strong>です。同じ日に賭け金0で記録している買い目（EVで選んでいない）は縮み1.000でした。</p>
</div>`;

function renderBets() {
  // 一覧は「買う + 推奨のみ」。成績用の _betsCache と取り違えないこと。
  const all = state._listCache || [];
  // 既定は「買うべき」だけ。総当たり812件では買うものを選べない。
  const bets = state.betsView === "all" ? all : all.filter(isRecommended);
  const f = state.filters.bets;

  // ── フィルター＆ソートエリア ──
  const filterArea = document.getElementById("bets-filter-area");
  filterArea.innerHTML = "";

  // 「買うべき」か「すべて」か。既定は買うべき。
  // 総当たり812件を並べても、そこから買うものを選べない。
  const nBuy = all.filter(isRecommended).length;
  const viewRow = document.createElement("div");
  viewRow.className = "view-toggle";
  viewRow.innerHTML = `
    <button class="view-btn${state.betsView === "buy" ? " active" : ""}" data-view="buy">
      各賭式の上位 <span class="view-btn__n">${nBuy}</span></button>
    <button class="view-btn${state.betsView === "all" ? " active" : ""}" data-view="all">
      すべて <span class="view-btn__n">${all.length}</span></button>`;
  filterArea.appendChild(viewRow);

  // ⚠️ 確度が高くても損益分岐は超えていない。ここを書かないと
  // 「S＝勝てる」と読まれる。必ず両方書く。
  const note = document.createElement("div");
  note.className = "buy-note";
  note.innerHTML = state.betsView === "buy"
    ? `<b>賭式ごとの最上位帯（S）だけ</b>を出しています。`
      + `段階は<b>その賭式の中での順位</b>なので、`
      + `同じSでも複勝は的中89%、3連単は15%です。`
      + `的中率は未使用データ10,809レースの実測（誤差±1〜2pt）。`
      + `<strong>⚠️ 最上位帯でも回収率は 87〜97% で、どれも100%未満です。`
      + `買えば平均して減ります。</strong>`
    : `全賭式・全レースの総当たり。<b>S</b>=最上位25% / <b>A</b>=上位25% / `
      + `<b>B</b>=中位 / <b>C</b>=下位25%（<b>その賭式の中での順位</b>）。`
      + `的中率は未使用データ10,809レースの実測。`;
  filterArea.appendChild(note);

  // ── 賭式ごとの内訳 ──
  // ⚠️ これを出す理由: 2026-09-03 まで、既定の一覧に拡連複・3連複・3連単が
  // **1件も出ていなかった**（絶対基準 的中率>=0.70 に永久に届かないため）。
  // 賭式が丸ごと消えていても、1本のリストを眺めているだけでは気づけない。
  // 各賭式が今日**何件あるか**を常に見えるようにして、0件なら0と表示する。
  const typeRow = document.createElement("div");
  typeRow.className = "type-summary";
  typeRow.innerHTML = BET_ORDER.map(bt => {
    const v = BET_TIER[bt] || {};
    const n = bets.filter(b => b.bet_type === bt).length;
    const c = CONFIDENCE[bt];
    const topHit = c ? c.hit[c.hit.length - 1] : null;
    const on = f.betType === betTypeLabel(bt);
    return `<button class="type-chip${on ? " active" : ""}${n ? "" : " type-chip--zero"}"
        data-bet-type="${betTypeLabel(bt)}"
        title="${v.tier}層・未見17,090レースでの実測回収率 ${v.roi}%。`
      + `${v.buy ? "実運用（賭け金を付ける）" : "ペーパー（記録のみ）"}。`
      + `最上位帯の実測的中率 ${topHit != null ? (topHit * 100).toFixed(0) + "%" : "―"}">
      <span class="type-chip__name">${betTypeLabel(bt)}</span>
      <span class="type-chip__n">${n}</span>
      <span class="type-chip__meta">${v.roi ? v.roi.toFixed(0) + "%" : ""}${
        v.buy ? "" : " ペーパー"}</span>
    </button>`;
  }).join("");
  filterArea.appendChild(typeRow);
  typeRow.querySelectorAll(".type-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      const t = btn.dataset.betType;
      state.filters.bets.betType = (f.betType === t) ? null : t;
      renderBets();
    });
  });

  // EV説明トグル
  const infoRow = document.createElement("div");
  infoRow.className = "bets-toolbar";
  infoRow.innerHTML = `
    <button class="ev-info-btn" id="ev-info-toggle" title="EVとは？">
      <span>EVとは？</span> <span id="ev-info-arrow">${state.evInfoOpen ? "▲" : "▼"}</span>
    </button>
    <div class="sort-toggle">
      <button class="sort-btn${state.betsSort === "prob" ? " active" : ""}" data-sort="prob">確率順</button>
      <button class="sort-btn${state.betsSort === "ev" ? " active" : ""}" data-sort="ev">EV順</button>
      <button class="sort-btn${state.betsSort === "race" ? " active" : ""}" data-sort="race">開催順</button>
    </div>`;
  filterArea.appendChild(infoRow);

  // EV説明パネル
  const infoPanel = document.createElement("div");
  infoPanel.id = "ev-info-panel";
  infoPanel.innerHTML = state.evInfoOpen ? EV_EXPLAIN_HTML : "";
  filterArea.appendChild(infoPanel);

  // 賭式フィルター。6賭式を1本の列に流すと1日800本を超えて探せないので、
  // 層（固い / 勝負 / 夢）と賭式で絞れるようにする。
  // 並びは BET_TIER の順（回収率の良い順）に固定する。名前順にすると
  // 「固い」と「夢」が混ざって層の意味が読めなくなる。
  const btOrder = Object.keys(BET_TIER);
  filterArea.appendChild(
    buildFilterBar(bets, b => betTypeLabel(b.bet_type), f.betType, val => {
      state.filters.bets.betType = val;
      state.betsShowAll = false;      // 絞り込み直したら表示上限を戻す
      renderBets();
    }, "全賭式", btOrder.map(betTypeLabel))
  );

  // 場別フィルター
  filterArea.appendChild(
    buildFilterBar(bets, b => b.stadium_name, f.stadium, val => {
      state.filters.bets.stadium = val;
      state.betsShowAll = false;
      renderBets();
    }, "全場")
  );

  // イベント
  document.getElementById("ev-info-toggle").addEventListener("click", () => {
    state.evInfoOpen = !state.evInfoOpen;
    renderBets();
  });
  filterArea.querySelectorAll(".sort-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      state.betsSort = btn.dataset.sort;
      renderBets();
    });
  });
  filterArea.querySelectorAll(".view-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      state.betsView = btn.dataset.view;
      // 表示を切り替えたら絞り込みも上限も戻す。前の表示で選んだ賭式が
      // 新しい表示に無いと「該当なし」になって理由が分からない。
      state.filters.bets.betType = null;
      state.filters.bets.stadium = null;
      state.betsShowAll = false;
      renderBets();
    });
  });

  // ── フィルター適用 ──
  let filtered = bets;
  if (f.betType) filtered = filtered.filter(b => betTypeLabel(b.bet_type) === f.betType);
  if (f.stadium) filtered = filtered.filter(b => b.stadium_name === f.stadium);

  // ── ソート ──
  // 「買うべき」は確率で選んでいるので、既定の並びも確率にする。
  // EV順にすると「EVで選んでいる」ように見えて、実測（EVで絞ると悪化）と
  // 食い違うメッセージになる。
  const bySort = (a, b) => {
    if (state.betsSort === "prob") {
      // 確度の段階が先、同じ段階なら帯の中での位置で比べる。
      // 生の確率順にすると複勝ばかりが上に来る（賭式で水準が違う）。
      // 実測の的中率そのもので並べる。賭式をまたいで比較できる唯一の量。
      const h = x => ((confidenceOf(x) || {}).hit || 0);
      const d = h(b) - h(a);
      if (d) return d;
      return (b.model_prob || 0) - (a.model_prob || 0);
    }
    if (state.betsSort === "ev") return (b.expected_value || 0) - (a.expected_value || 0);
    if (a.stadium_name !== b.stadium_name) return a.stadium_name.localeCompare(b.stadium_name, "ja");
    if (a.race_no !== b.race_no) return a.race_no - b.race_no;
    return (b.expected_value || 0) - (a.expected_value || 0);
  };
  // 確定済みと未確定を分ける。日中に開いたとき最初に知りたいのは
  // 「次に買うのはどれか」であり、終わったレースと混ざっていると探せない。
  const settledOf = b => b.is_hit === true || b.is_hit === false;
  // 確定した買い目（締切間近で固定＝もう更新されない）を最上段に置く。
  // 「今これを買えばよい」が一目で分かるようにするため。
  const finalOf = b => !settledOf(b) && b.is_final_pick;
  const finals   = filtered.filter(finalOf).sort(bySort);
  const upcoming = filtered.filter(b => !settledOf(b) && !finalOf(b)).sort(bySort);
  const settled  = filtered.filter(settledOf).sort(bySort);
  filtered = [...finals, ...upcoming, ...settled];

  // ── サマリー ──
  // 日次の合計は上の day-panel が持つため、ここは「絞り込みの結果」だけを出す。
  // 同じ金額を2箇所に出すと、どちらを見ればよいか分からなくなる。
  // ⚠️ 金額は買う買い目だけで数える。推奨のみは賭け金0なので足しても
  // 変わらないが、条件を明示しておかないと将来 record に金額が付いたときに
  // 黙って混ざる（8/30 に実際そうなった）。
  const buyN = filtered.filter(isPurchased).length;
  const totalAmt = filtered.filter(isPurchased)
    .reduce((s, b) => s + (b.recommended_amount || 0), 0);
  const isFiltered = filtered.length !== bets.length;
  document.getElementById("bets-summary").innerHTML = filtered.length ? `
    <div class="bets-summary">
      <span>${isFiltered ? `絞り込み <strong>${filtered.length}</strong>/${bets.length}`
                         : `<strong>${filtered.length}</strong>`} 件</span>
      <span>うち買う <strong>${buyN}</strong> 件</span>
      <span>投資予定 <strong>¥${totalAmt.toLocaleString()}</strong></span>
    </div>` : "";

  // ── カード描画（EV順のときはティア区切りを挿入）──
  const container = document.getElementById("bet-list");
  if (!filtered.length) {
    container.innerHTML = '<div class="empty">該当する買い目がありません</div>';
    return;
  }

  // 1日6賭式で800本を超える。全部を一度に描くと目的の1本まで辿り着けないので、
  // 先頭から一定数だけ描き、残りはボタンで開く。区切りの件数は絞り込み後の
  // 全体（finals/upcoming/settled の長さ）を出すので、上限で数字は変わらない。
  const RENDER_CAP = 60;
  const shown = state.betsShowAll ? filtered : filtered.slice(0, RENDER_CAP);
  const hiddenN = filtered.length - shown.length;

  let html = "";
  let lastTier = null;
  let section = null;
  shown.forEach((b, i) => {
    const sec = settledOf(b) ? "settled" : (finalOf(b) ? "final" : "upcoming");
    if (sec !== section) {
      if (sec === "final") {
        html += `<div class="sec-divider sec-divider--final">買い目確定 <span class="sec-divider__n">${finals.length}件・締切間近</span></div>`;
      } else if (sec === "upcoming" && (finals.length || settled.length)) {
        html += `<div class="sec-divider sec-divider--live">これから <span class="sec-divider__n">${upcoming.length}件・オッズ次第で変わります</span></div>`;
      } else if (sec === "settled") {
        const hitN = settled.filter(x => x.is_hit === true).length;
        html += `<div class="sec-divider">レース済 <span class="sec-divider__n">${settled.length}件・的中 ${hitN}</span></div>`;
      }
      section = sec;
      lastTier = null;   // 区切りをまたいだらEV帯の見出しを出し直す
    }
    if (state.betsSort === "ev") {
      const ev = b.expected_value || 0;
      // 区切りは締切前の板での実測に対応させる（evColor のコメント参照）。
      // ⚠️ 以前は確定オッズで測った数字を出しており、高EV帯に
      // 「回収率 239〜280%」と書いていた。実際は逆で、絞るほど悪化する。
      const tier =
        ev >= 2.5 ? "EV 2.5以上　実測 回収率 48.9%" :
        ev >= 2.0 ? "EV 2.0〜2.5　実測 54.1%" :
        ev >= 1.5 ? "EV 1.5〜2.0　実測 63.4%" :
                    "EV 1.5未満　実測 71.6%";
      const tierColor = evColor(ev);
      if (tier !== lastTier) {
        html += `<div class="ev-tier-divider" style="color:${tierColor}">${tier}</div>`;
        lastTier = tier;
      }
    }
    html += buildBetCard(b, i);
  });
  if (hiddenN > 0) {
    html += `<button class="show-more" id="bets-show-more">
      残り ${hiddenN} 件を表示</button>`;
  }
  container.innerHTML = html;
  container.querySelectorAll(".bet-card").forEach((el, i) => {
    // 第3引数は race_id が引けなかったときの保険（openRaceModal 参照）
    el.addEventListener("click", () => openRaceModal(shown[i].race_id,
      `${shown[i].stadium_name} R${shown[i].race_no}`,
      { stadium: shown[i].stadium_name, race_no: shown[i].race_no }));
  });
  const more = document.getElementById("bets-show-more");
  if (more) more.addEventListener("click", () => {
    state.betsShowAll = true;
    renderBets();
  });
}

function buildBetCard(b) {
  const ev = b.expected_value ?? 0;
  const color = evColor(ev);
  const hitCls = b.is_hit === true ? "settled-hit" : b.is_hit === false ? "settled-miss" : "";
  // 結果が出たレースは着順も出す。当たり外れだけでは「何着だったか」が分からず、
  // 惜しかったのか全く違ったのかが判断できない。
  const order = Array.isArray(b.result_order) ? b.result_order.slice(0, 3) : null;
  const orderHtml = order && order.length
    ? `<span class="result-order">結果 ${order.map((n, i) =>
        `<span class="bn bn-${n} bn-sm">${n}</span>`).join('<span class="ord-sep">›</span>')}</span>`
    : "";
  const hitLabel = b.is_hit === true
    ? `<span class="val-good">✓ 的中 +¥${payoutOf(b).toLocaleString()}</span>`
    : b.is_hit === false ? `<span class="val-bad">✗ 外れ</span>` : "";
  const raceTypeShort = b.race_type
    ? b.race_type.replace("レディース/", "L/").replace("予選", "予").replace("準優勝戦", "準優").replace("優勝戦", "優")
    : "";

  // 締切間近で固定された買い目。以後オッズが動いても更新されないので
  // 「これを買えばよい」と判断できる。朝の買い目はあくまで目安。
  const finalBadge = b.is_final_pick && b.is_hit == null
    ? `<span class="pick-final">買い目確定</span>` : "";

  return `
    <div class="bet-card ${hitCls}${b.is_final_pick && b.is_hit == null ? " is-final" : ""}" style="cursor:pointer;border-left-color:${color}">
      <div class="bet-card__head">
        <div class="bet-card__race">
          ${gradeBadge(b.grade)}
          ${categoryBadges(b.race_type, b.is_night)}
          <span>${b.stadium_name} R${b.race_no}</span>
          ${raceTypeShort ? `<span class="race-type-label">${raceTypeShort}</span>` : ""}
          ${b.closing_time ? `<span class="close-time">⏱${b.closing_time}</span>` : ""}
          ${finalBadge}
        </div>
        <span class="bet-card__ev" style="color:${color}">EV ${ev.toFixed(2)}
          ${b.rule === RECORD_RULE ? ""
            : `<span class="ev-real">実質 ${realEv(ev, b).toFixed(2)}</span>`}</span>
      </div>
      <div class="bet-card__body">
        <div class="bet-card__combo">
          <span class="bet-type-label">${betTypeLabel(b.bet_type)}</span>
          ${confBadge(b)}
          ${comboSpans(b.combination)}
        </div>
        <span class="bet-card__amount">${isPurchased(b)
          ? "¥" + (b.recommended_amount||0).toLocaleString()
          : `<span class="amount-record">推奨のみ</span>`}</span>
      </div>
      <div class="bet-card__foot">
        <span class="bet-card__stats">
          確率 <b>${((b.model_prob||0)*100).toFixed(1)}%</b>
          <span class="dot-sep">·</span>
          オッズ <b>${oddsText(b)}</b>${oddsMark(b.odds)}
        </span>
        ${hitLabel}
      </div>
      ${orderHtml ? `<div class="bet-card__result">${orderHtml}</div>` : ""}
    </div>`;
}

// 複勝と拡連複の板は `1.0-1.3` の**範囲**で出る。「誰と一緒に2着(3着)以内に
// 入るか」で配当が変わるため、買う時点では1つに決まらない。
// ⚠️ 2026-09-03 まで下限だけを `オッズ 1.0x` と表示していた。実際には
// 1.6倍返ってくることがあり、画面が嘘をついているように見えていた。
// 実測（8/31-9/3 の当たり369本）: 下限1.0 の実払戻は 1.00〜17.90倍、
// 元返しだったのは60.4%。**下限は元返しの予告ではない。**
function oddsText(b) {
  const lo = b.odds || 0;
  const hi = b.odds_upper;
  if (hi != null && hi > lo + 0.001) {
    return `${lo.toFixed(1)}-${hi.toFixed(1)}x`;
  }
  return `${lo.toFixed(1)}x`;
}

// 実測(未見データ2期間)で、オッズが高い帯ほど回収率が良い:
//   1.5〜3倍 96〜136% / 3〜5倍 113〜124% / 5〜8倍 123〜150%
//   8〜15倍 187〜272% / 15〜50倍 493%(5-6月, n=26)
// カードを眺めるだけでは気づけない差なので、優位な帯に印をつける。
// 高オッズは本数が少なく振れも大きいため、煽らず控えめな印にとどめる。
function oddsMark(odds) {
  const o = odds || 0;
  if (o >= 8) return `<span class="odds-mark odds-mark--hot" title="実測でこの帯は回収率が高い（ただし本数は少なく振れも大きい）">妙味</span>`;
  if (o >= 5) return `<span class="odds-mark" title="実測で回収率がやや高い帯">◦</span>`;
  return "";
}

// ════════════════════════════════
// レースページ
// ════════════════════════════════
async function loadRaces() {
  const container = document.getElementById("race-list");
  container.innerHTML = '<div class="empty">読込中…</div>';
  state._racesCache = [];
  state._betCountByRace = {};
  try {
    const [races, bets] = await Promise.all([
      api(`data/races_${state.date}.json`),
      api(`data/bets_${state.date}.json`).catch(() => []),
    ]);
    state._racesCache = races;

    // 買い目数マップ
    state._betCountByRace = {};
    bets.forEach(b => {
      state._betCountByRace[b.race_id] = (state._betCountByRace[b.race_id] || 0) + 1;
    });

    if (!races.length) {
      document.getElementById("races-filter-area").innerHTML = "";
      container.innerHTML = '<div class="empty">この日の開催データがありません</div>';
      return;
    }

    renderRaces();
    races.forEach(r => loadRaceProbs(r.id));
  } catch (e) {
    document.getElementById("races-filter-area").innerHTML = "";
    container.innerHTML = e.message === "404"
      ? '<div class="empty">この日のデータがありません</div>'
      : `<div class="empty">取得失敗 (${e.message})</div>`;
  }
}

function renderRaces() {
  const races = state._racesCache;
  const f = state.filters.races;

  // フィルターエリア：場別 + グレード別
  const filterArea = document.getElementById("races-filter-area");
  filterArea.innerHTML = "";

  // 場別
  filterArea.appendChild(
    buildFilterBar(races, r => r.stadium, f.stadium, val => {
      state.filters.races.stadium = val;
      renderRaces();
      races.forEach(r => loadRaceProbs(r.id));
    }, "全場")
  );

  // グレード別（一般以外があるときだけ表示）
  const nonGenRaces = races.filter(r => r.grade && r.grade !== "一般");
  if (nonGenRaces.length > 0) {
    const gradeBar = buildFilterBar(
      races.filter(r => r.grade),
      r => r.grade,
      f.grade,
      val => {
        state.filters.races.grade = val;
        renderRaces();
        races.forEach(r => loadRaceProbs(r.id));
      },
      "全グレード"
    );
    gradeBar.classList.add("filter-bar--secondary");
    filterArea.appendChild(gradeBar);
  }

  // フィルター適用
  let filtered = races;
  if (f.stadium) filtered = filtered.filter(r => r.stadium === f.stadium);
  if (f.grade)   filtered = filtered.filter(r => r.grade === f.grade);

  const container = document.getElementById("race-list");
  if (!filtered.length) {
    container.innerHTML = '<div class="empty">該当するレースがありません</div>';
    return;
  }

  container.className = "card-list grid-2";
  container.innerHTML = filtered.map(r => buildRaceCard(r)).join("");
  container.querySelectorAll(".race-card").forEach((el, i) => {
    el.addEventListener("click", () => openRaceModal(filtered[i].id,
      `${filtered[i].stadium} R${filtered[i].race_no}`));
  });
}

function buildRaceCard(r) {
  const betCount = state._betCountByRace?.[r.id] || 0;
  const betBadge = betCount > 0
    ? `<span class="badge badge-bets">推奨${betCount}件</span>`
    : "";

  // 終了したレースは着順を出す。従来は出走表と予測だけで、結果を見るには
  // 買い目ページへ戻る必要があった（買い目の無いレースは確認手段が無かった）。
  const order = Array.isArray(r.result_order) ? r.result_order.slice(0, 3) : null;
  const done = order && order.length >= 3;
  const resultHtml = done
    ? `<span class="result-order">結果 ${order.map(n =>
        `<span class="bn bn-${n} bn-sm">${n}</span>`).join('<span class="ord-sep">›</span>')}</span>`
    : "";

  // 予測1着と実際の1着が一致したか（モデルの当たり外れが一目で分かる）
  const preds = r.predictions ?? [];
  const top = preds.length
    ? [...preds].sort((a, b) => b.win_prob - a.win_prob)[0].boat_no
    : null;
  const mark = done && top
    ? (top === order[0]
        ? `<span class="pred-mark pred-mark--ok" title="予測1着が的中">予想的中</span>`
        : `<span class="pred-mark" title="予測1着は ${top}号艇だった">予想 ${top}</span>`)
    : "";

  return `
    <div class="race-card${betCount > 0 ? " has-bets" : ""}${done ? " is-done" : ""}" style="cursor:pointer">
      <div class="race-card__head">
        <span class="race-card__title">${r.stadium} R${r.race_no}</span>
        <div class="race-card__meta">
          ${gradeBadge(r.grade)}
          ${categoryBadges(r.race_type, r.is_night)}
          ${r.closing_time ? `<span>⏱${r.closing_time}</span>` : ""}
        </div>
      </div>
      <div class="prob-row" id="prob-${r.id}">
        <span style="color:var(--muted);font-size:.75rem;">読込中…</span>
      </div>
      ${resultHtml || betBadge ? `
        <div class="race-card__foot">
          ${resultHtml}${mark}
          ${betBadge}
        </div>` : ""}
    </div>`;
}

function loadRaceProbs(raceId) {
  const row = document.getElementById(`prob-${raceId}`);
  if (!row) return;
  const race = state._racesCache.find(r => r.id === raceId);
  const preds = race?.predictions ?? [];
  const top = [...preds].sort((a, b) => b.win_prob - a.win_prob).slice(0, 4);
  row.innerHTML = top.map(p =>
    `<span class="prob-chip">${bn(p.boat_no)} ${(p.win_prob*100).toFixed(0)}%</span>`
  ).join("") || '<span style="color:var(--muted);font-size:.75rem;">予測なし</span>';
}

// ════════════════════════════════
// レース詳細モーダル
// ════════════════════════════════
function openRaceModal(raceId, title, fallback) {
  // race_id はファイルをまたぐと当てにならない。クラウド(predict_cloud)は
  // その日ぶんの使い捨てSQLiteで JSON を書くので採番が別体系になり、
  // 2026-08-26 実測で クラウド 73〜 / 履歴DB 36936〜 と重なりゼロだった。
  // bets と races の片方だけが差し替わった組でも引けるよう、
  // 場とレース番号で拾い直す（src/export.py の _race_key と同じ考え方）。
  let race = state._racesCache.find(r => r.id === raceId);
  if (!race && fallback) {
    race = state._racesCache.find(r => r.stadium === fallback.stadium
                                    && r.race_no === fallback.race_no);
  }
  const entries = race?.entries ?? [];
  const preds   = race?.predictions ?? [];
  // モーダルは6賭式すべて出す（_listCache）。ここを _betsCache にしていると
  // 「2連複しか出ない」が一覧とモーダルの両方で起きる。
  // race_id は経路によって採番が違うので、上の race と同じく場+R番号でも拾う。
  let raceBets = (state._listCache || []).filter(b => b.race_id === raceId);
  if (!raceBets.length && fallback) {
    raceBets = (state._listCache || []).filter(
      b => b.stadium_name === fallback.stadium && b.race_no === fallback.race_no);
  }
  const predMap  = Object.fromEntries(preds.map(p => [p.boat_no, p]));

    const entryRows = entries.map(e => {
      const p = predMap[e.boat_no] || {};
      return `<tr>
        <td>${bn(e.boat_no)}</td>
        <td>${e.racer_name || "—"}</td>
        <td>${e.racer_class || "—"}</td>
        <td>${(e.national_win_rate||0).toFixed(2)}</td>
        <td>${(e.motor_top2_rate||0).toFixed(1)}%</td>
        <td style="font-weight:600;color:var(--accent-lt)">${
          p.win_prob !== undefined ? (p.win_prob*100).toFixed(1)+"%" : "—"
        }</td>
      </tr>`;
    }).join("");

    // 賭式は「固い→夢」の順で並べる。回収率の良い順なので、上から読めば
    // 手堅い順に見える。BET_TIER に無い賭式は末尾へ。
    const btRank = Object.keys(BET_TIER);
    const sortedBets = [...raceBets].sort((x, y) => {
      const rx = btRank.indexOf(x.bet_type), ry = btRank.indexOf(y.bet_type);
      return (rx < 0 ? 99 : rx) - (ry < 0 ? 99 : ry);
    });
    const betsSection = sortedBets.length ? `
      <div style="margin-top:1rem;">
        <p style="font-size:.78rem;color:var(--muted);margin-bottom:.4rem;">推奨買い目</p>
        ${sortedBets.map(b => `
          <div style="display:flex;justify-content:space-between;align-items:center;gap:.4rem;
                      padding:.35rem 0;border-bottom:1px solid var(--surface2);font-size:.85rem;">
            <span>${betTypeLabel(b.bet_type)}${tierBadge(b.bet_type)} ${comboSpans(b.combination)}</span>
            <span style="color:${evColor(b.expected_value||0)};font-weight:700;">EV ${(b.expected_value||0).toFixed(2)}</span>
            <span style="color:${isPurchased(b) ? "var(--green)" : "var(--muted)"};white-space:nowrap;">${
              isPurchased(b) ? "¥" + (b.recommended_amount||0).toLocaleString() : "推奨のみ"}</span>
          </div>`).join("")}
      </div>` : "";

  // 出走表が無いとき、表の枠だけ出ると「黙って空」になり原因が分からない。
  // 2026-08-26 に実際そうなった（判定が races JSON を空で上書きしていた）。
  // 出ない理由が見えるようにしておく。
  const tableHtml = entries.length ? `
    <table class="entry-table">
      <thead><tr>
        <th>枠</th><th style="text-align:left">選手</th><th>級</th>
        <th>全勝率</th><th>M2連</th><th>1着%</th>
      </tr></thead>
      <tbody>${entryRows}</tbody>
    </table>` : `
    <div class="empty" style="padding:1rem 0;">
      このレースの出走表が取れていません<br>
      <span style="font-size:.75rem;color:var(--muted);">
        データの更新待ちか、書き出しに失敗しています
      </span>
    </div>`;

  openModal(`
    <h3 style="font-weight:700;margin-bottom:.75rem;">${title}</h3>
    ${tableHtml}
    ${betsSection}
  `);
}

// ════════════════════════════════
// 収支ページ
// ════════════════════════════════
// ── 収支タブ ──
// 2026-08-11: 独立ページだった pdca.html をここに統合した。
//   別ファイル・独自CSS(2,476字)・独自JS(6,957字)で二重管理になっており、
//   スマホでは画面遷移も挟まって見づらかった。
let _perfWindow = "30d";

async function loadPerf() {
  const container = document.getElementById("perf-content");
  container.innerHTML = '<div class="empty">読込中…</div>';
  try {
    const [pdca, perf] = await Promise.all([
      api("data/pdca.json").catch(() => null),
      api("data/performance.json").catch(() => null),
    ]);
    if (!pdca && !perf) {
      container.innerHTML = '<div class="empty">実績データがまだありません</div>';
      return;
    }
    renderPerf(container, pdca, perf);
  } catch (e) {
    container.innerHTML = `<div class="empty">取得失敗 (${e.message})</div>`;
  }
}

function renderPerf(container, pdca, perf) {
  const yen = n => (n == null ? "—" : (n < 0 ? "−" : "") + "¥" + Math.abs(Math.round(n)).toLocaleString());
  const pct = (n, d = 1) => (n == null ? "—" : (n * 100).toFixed(d) + "%");
  const W = { "7d": "直近7日", "30d": "直近30日", "all": "全期間" };

  let html = "";

  if (pdca && pdca.windows) {
    const w = pdca.windows[_perfWindow] || pdca.windows["30d"] || pdca.windows["all"];
    const t = (w && w.total) || {};
    const good = (t.profit || 0) >= 0;

    html += `
      <div class="seg-group" id="perf-window">
        ${Object.keys(W).filter(k => pdca.windows[k]).map(k =>
          `<button class="seg${k === _perfWindow ? " active" : ""}" data-w="${k}">${W[k]}</button>`).join("")}
      </div>`;

    if (t.bets) {
      html += `
        <section class="day-panel ${good ? "day-panel--good" : "day-panel--bad"}" style="margin-top:.7rem">
          <div class="day-panel__head">
            <span class="day-panel__title">${W[_perfWindow]}の${state._paper ? "仮の損益" : "損益"}</span>
            <span class="chip">${t.bets} 買い目</span>
          </div>
          <div class="day-hero">
            <div class="day-hero__value ${good ? "is-good" : "is-bad"}">${good ? "+" : "−"}¥${Math.abs(Math.round(t.profit || 0)).toLocaleString()}</div>
            <div class="day-hero__label">${state._paper ? "仮の損益（賭けていません）"
              : good ? "▲ 損益（プラス）" : "▼ 損益（マイナス）"}</div>
          </div>
          <div class="stat-row">
            <div class="stat"><div class="stat__val">${pct(t.roi, 1)}</div><div class="stat__lab">回収率</div></div>
            <div class="stat"><div class="stat__val">${t.hits}<span class="stat__sub">/${t.bets}</span></div><div class="stat__lab">的中</div></div>
            <div class="stat"><div class="stat__val">${pct(t.hit_rate, 1)}</div><div class="stat__lab">的中率</div></div>
            <div class="stat"><div class="stat__val">${yen(t.invested)}</div><div class="stat__lab">投資</div></div>
          </div>
        </section>`;
    } else {
      html += `<div class="empty" style="margin-top:.7rem">${W[_perfWindow]}の判定済み買い目はありません</div>`;
    }

    if (pdca.daily && pdca.daily.length) {
      html += `<h3 class="info-heading">累積損益</h3><div id="perf-chart"></div>`;
    }

    // 買い式別は2種類以上あるときだけ（1種類なら上のサマリと同じ内容になる）
    const bt = (w && w.by_bet_type) || {};
    if (Object.keys(bt).length > 1) {
      html += `<h3 class="info-heading">買い式別</h3>
        <div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>買い式</th><th>本数</th><th>的中率</th><th>回収率</th><th>損益</th></tr></thead>
          <tbody>${Object.entries(bt).map(([k, a]) => `
            <tr><td>${betTypeLabel(k)}</td><td>${a.bets}</td><td>${pct(a.hit_rate, 1)}</td>
              <td class="${a.roi >= 1 ? "val-good" : "val-bad"}">${pct(a.roi, 1)}</td>
              <td class="${a.profit >= 0 ? "val-good" : "val-bad"}">${yen(a.profit)}</td></tr>`).join("")}
          </tbody></table></div>`;
    }

    // 確率帯別: 買い目の選び方が効いているかを見る中核
    if (pdca.band_hit_rates && pdca.band_hit_rates.length) {
      html += `<h3 class="info-heading">確率帯別（直近30日）</h3>
        <div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>帯</th><th>本数</th><th>予測</th><th>実際</th><th>回収率</th></tr></thead>
          <tbody>${pdca.band_hit_rates.map(b => `
            <tr><td>${b.band}</td><td>${b.n}</td><td>${pct(b.avg_model_prob, 1)}</td>
              <td>${pct(b.hit_rate, 1)}</td>
              <td class="${b.roi >= 1 ? "val-good" : "val-bad"}">${pct(b.roi, 1)}</td></tr>`).join("")}
          </tbody></table></div>`;
    }

    const daily = (pdca.daily || []).filter(d => d.total && d.total.bets).slice(0, 30);
    if (daily.length) {
      html += `<h3 class="info-heading">日次</h3>
        <div class="tbl-wrap"><table class="tbl">
          <thead><tr><th>日付</th><th>本数</th><th>的中</th><th>回収率</th><th>損益</th></tr></thead>
          <tbody>${daily.map(d => `
            <tr><td>${d.date.slice(5)}</td><td>${d.total.bets}</td><td>${d.total.hits}</td>
              <td class="${d.total.roi >= 1 ? "val-good" : "val-bad"}">${pct(d.total.roi, 0)}</td>
              <td class="${d.total.profit >= 0 ? "val-good" : "val-bad"}">${yen(d.total.profit)}</td></tr>`).join("")}
          </tbody></table></div>`;
    }
  }

  const b = perf && perf.backtest;
  if (b) {
    html += `<h3 class="info-heading">バックテスト参考値</h3>
      <p class="info-note" style="margin:-.2rem 0 .5rem">${b.date_start} 〜 ${b.date_end}</p>
      <div class="stat-row">
        <div class="stat"><div class="stat__val">${pct(b.roi, 1)}</div><div class="stat__lab">回収率</div></div>
        <div class="stat"><div class="stat__val">${pct(b.hit_rate, 1)}</div><div class="stat__lab">的中率</div></div>
        <div class="stat"><div class="stat__val">${(b.bet_races || 0).toLocaleString()}</div><div class="stat__lab">購入レース</div></div>
        <div class="stat"><div class="stat__val">${pct(b.max_drawdown, 1)}</div><div class="stat__lab">最大下落</div></div>
      </div>`;
  }

  container.innerHTML = html;

  const seg = document.getElementById("perf-window");
  if (seg) seg.querySelectorAll(".seg").forEach(btn =>
    btn.addEventListener("click", () => {
      _perfWindow = btn.dataset.w;
      renderPerf(container, pdca, perf);
    }));

  if (pdca && pdca.daily) renderCumChart(pdca.daily);
}

// 累積損益の折れ線。判定済みの日だけを古い順に積み上げる。
// 線は2px、基準線は実線で控えめに、終点だけ点を置く。
// 各点に不可視の当たり判定を重ね、触れると日付と累積額が出る。
function renderCumChart(daily) {
  const host = document.getElementById("perf-chart");
  if (!host) return;
  const pts = [...daily]
    .filter(d => d.total && d.total.hits != null && d.total.bets)
    .sort((a, b) => a.date.localeCompare(b.date));
  if (!pts.length) { host.innerHTML = '<div class="empty">まだ判定済みの日がありません</div>'; return; }

  let cum = 0;
  const series = pts.map(d => ({ date: d.date, cum: (cum += d.total.profit || 0) }));
  const W = 640, H = 200, PL = 8, PR = 8, PT = 16, PB = 22;
  const ys = series.map(p => p.cum);
  const yMin = Math.min(0, ...ys), yMax = Math.max(0, ...ys);
  const range = (yMax - yMin) || 1;
  const X = i => PL + (W - PL - PR) * (series.length === 1 ? 0.5 : i / (series.length - 1));
  const Y = v => H - PB - (H - PT - PB) * (v - yMin) / range;
  const last = series[series.length - 1].cum;
  const good = last >= 0;
  const stroke = good ? "var(--green)" : "var(--red)";
  const line = series.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.cum).toFixed(1)}`).join(" ");
  const area = `${line} L${X(series.length - 1).toFixed(1)},${Y(0).toFixed(1)} L${X(0).toFixed(1)},${Y(0).toFixed(1)} Z`;

  host.innerHTML = `
    <div class="chart">
      <svg viewBox="0 0 ${W} ${H}" role="img"
           aria-label="累積損益の推移。${series[0].date} から ${series[series.length - 1].date} まで、最終 ${Math.round(last).toLocaleString()} 円">
        <line class="chart__base" x1="${PL}" x2="${W - PR}" y1="${Y(0)}" y2="${Y(0)}"/>
        <path d="${area}" fill="${stroke}" opacity=".10"/>
        <path d="${line}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${X(series.length - 1)}" cy="${Y(last)}" r="4" fill="${stroke}" class="chart__end"/>
        ${series.map((p, i) => `<circle class="chart__hit" cx="${X(i)}" cy="${Y(p.cum)}" r="9"><title>${p.date}　${p.cum >= 0 ? "+" : "−"}¥${Math.abs(Math.round(p.cum)).toLocaleString()}</title></circle>`).join("")}
      </svg>
      <div class="chart__foot">
        <span>${series[0].date.slice(5)}</span>
        <span class="${good ? "val-good" : "val-bad"}">累積 ${good ? "+" : "−"}¥${Math.abs(Math.round(last)).toLocaleString()}（${series.length}日）</span>
        <span>${series[series.length - 1].date.slice(5)}</span>
      </div>
    </div>`;
}

// ════════════════════════════════
// 設定ページ
// ════════════════════════════════
function loadSettings() {
  // 2026-08-11: 「EV ≥ 1.20」しか出ておらず、実際の選別条件
  // (的中確率 30% 以上) が見えなかったため実態に合わせた。
  const container = document.getElementById("settings-content");
  const rows = [
    ["買い式", "2連複のみ", "3連単・3連複は黒字条件が見つからず停止中"],
    ["的中確率", "30% 以上", "低確信度帯は的中率が大きく落ちるため除外"],
    ["期待値", "1.20 以上", "確率 × オッズ"],
    ["オッズ", "1.5 〜 50 倍", ""],
    ["賭け金", "500 円 / 本", "定額。1日の上限 15,000 円（期待値の高い順に配分）"],
    ["1レース上限", "1,000 円", ""],
    ["停止条件", "25連敗 / 下落45%", "実測で最長12〜14連敗・最大下落31%が正常範囲のため"],
  ];
  container.innerHTML = `
    <h3 class="info-heading" style="margin-top:0">いま動いている条件</h3>
    <div class="info-card">
      <div class="info-card__label">データ更新</div>
      <div class="info-card__value" style="font-size:.9rem">毎朝 8:00 収集・予測生成 → 日中毎時オッズ更新 → 22:30 判定</div>
      <div class="info-card__sub">GitHub Pages 経由で配信</div>
    </div>
    ${rows.map(([label, value, sub]) => `
      <div class="info-card">
        <div class="info-card__label">${label}</div>
        <div class="info-card__value">${value}</div>
        ${sub ? `<div class="info-card__sub">${sub}</div>` : ""}
      </div>`).join("")}`;
}

// ════════════════════════════════
// ページロード
// ════════════════════════════════
function loadPage(page) {
  _lastLoad = Date.now();   // 自動更新の起点。手動操作の直後は再取得しない
  if (page === "bets")     loadBets();
  if (page === "races")    loadRaces();
  if (page === "perf")     loadPerf();
  // 「設定」と「情報」は1ページに統合済み（現在の運用条件 → モデルの中身、の順）
  if (page === "info")     { loadSettings(); renderInfoPage(); }
}

function renderInfoPage() {
  // 2026-08-11: 実態と乖離していたため全面的に書き直した。
  //   旧: ロジスティック回帰 / 特徴量45項目 / 3賭式 / 的中率55.5%等
  //   実: LambdaRank+Plackett-Luce / 30項目 / 2連複のみ / 下記の実測値
  const FEATURES = {
    "レース情報": ["レースNo", "グレード", "ナイター", "距離", "月", "開催場", "艇番", "進入コース"],
    "選手": ["級別", "年齢", "体重", "F回数", "L回数", "反則計", "平均ST",
             "全国勝率", "全国2連率", "全国3連率", "当地勝率", "当地2連率", "当地3連率",
             "全国勝率Z", "当地勝率Z"],
    "モーター・ボート": ["モーター2連率", "モーター3連率", "モーター2連率Z",
                        "ボート2連率", "ボート3連率", "ボート2連率Z"],
    "会場": ["海水フラグ"],
  };

  const featHtml = Object.entries(FEATURES).map(([group, tags]) => `
    <div class="info-feat-group">
      <div class="info-feat-title">${group}</div>
      <div class="info-feat-tags">
        ${tags.map(t => `<span class="info-tag impl">${t}</span>`).join("")}
      </div>
    </div>`).join("");

  document.getElementById("info-content").innerHTML = `

    <div class="info-section">
      <h3 class="info-heading">予測モデル</h3>
      <p class="info-text">
        <strong>LambdaRank（ランキング学習）+ Plackett-Luce</strong> を使用しています。
        各艇の「強さスコア」を学習し、Plackett-Luce モデルで着順の同時確率を導出するため、
        独立モデルの掛け算による誤差の累積が起きません。
      </p>
      <div class="info-accuracy">
        <div class="info-acc-item">
          <div class="info-acc-val">64.2%</div>
          <div class="info-acc-label">1着を当てる率</div>
          <div class="info-acc-base">ランダム 16.7%</div>
        </div>
        <div class="info-acc-item">
          <div class="info-acc-val">30.9%</div>
          <div class="info-acc-label">買い目の的中率</div>
          <div class="info-acc-base">未見データ 1,101本</div>
        </div>
        <div class="info-acc-item">
          <div class="info-acc-val">147%</div>
          <div class="info-acc-label">回収率</div>
          <div class="info-acc-base">損益分岐 100%</div>
        </div>
        <div class="info-acc-item">
          <div class="info-acc-val">16%</div>
          <div class="info-acc-label">最大下落幅</div>
          <div class="info-acc-base">1,000円/本の場合</div>
        </div>
      </div>
      <p class="info-note">
        ※ 回収率・的中率は「そのレースより前のデータだけで学習したモデル」を
        5月 / 6月 / 7-8月 の3期間で検証した実測値（全期間で黒字）。
        月ごとの回収率は 115〜179% と振れます。
      </p>
    </div>

    <div class="info-section">
      <h3 class="info-heading">買い目の選び方</h3>
      <p class="info-text">
        <strong>2連複のみ・的中確率 30% 以上・期待値 1.2 以上</strong>。
        期待値だけで選ぶと、モデルの推定が上振れした買い目ばかりを拾ってしまい
        （実測で予測確率が実態の約1.8倍に膨張）、回収率が落ちます。
        確信度で足切りしたうえで期待値を見る、この組み合わせだけが
        検証した3期間すべてで黒字でした。
      </p>
      <div class="info-schedule">
        <div class="info-sched-row"><span class="info-sched-time">2連複</span><span class="info-sched-desc">3連単・3連複は黒字条件が見つからず停止中</span></div>
        <div class="info-sched-row"><span class="info-sched-time">確率</span><span class="info-sched-desc">30% 以上（低確信度帯は的中率が大きく落ちる）</span></div>
        <div class="info-sched-row"><span class="info-sched-time">期待値</span><span class="info-sched-desc">1.2 以上（オッズ 1.5〜50 倍）</span></div>
        <div class="info-sched-row"><span class="info-sched-time">賭け金</span><span class="info-sched-desc">定額 500 円 / 本（1日の上限 15,000 円）</span></div>
      </div>
    </div>

    <div class="info-section">
      <h3 class="info-heading">使用している特徴量（30項目）</h3>
      <div class="info-features">${featHtml}</div>
      <p class="info-note">
        ※ 直前情報・気象は 2026/5/21 に取得を終了したため、現在は使用していません。
      </p>
    </div>

    <div class="info-section">
      <h3 class="info-heading">更新スケジュール</h3>
      <div class="info-schedule">
        <div class="info-sched-row"><span class="info-sched-time">08:00</span><span class="info-sched-desc">データ収集・全レース予測生成・自動Push</span></div>
        <div class="info-sched-row"><span class="info-sched-time">08:45</span><span class="info-sched-desc">オッズ退避（PC停止時の保険・クラウド）</span></div>
        <div class="info-sched-row"><span class="info-sched-time">日中毎時</span><span class="info-sched-desc">オッズ自動更新（GitHub Actions）</span></div>
        <div class="info-sched-row"><span class="info-sched-time">22:30</span><span class="info-sched-desc">結果収集・的中判定・実績更新</span></div>
      </div>
    </div>
  `;
}

// ════════════════════════════════
// 自動更新
// ════════════════════════════════
// クラウド側が日中1時間ごとにオッズ更新と結果判定を push している。
// アプリは読み込み時に一度取るだけだったので、開いたままでは新しい結果が
// 反映されなかった。当日を見ている間だけ定期的に取り直す。
//   - 他の日を見ているときは更新しない（過去日は変わらない）
//   - 画面が隠れている間は止める（無駄な通信と電池消費を避ける）
//   - 復帰時は、最後の取得から間が空いていれば即座に取り直す
const REFRESH_MS = 5 * 60 * 1000;
let _lastLoad = Date.now();
let _refreshTimer = null;

function refreshIfStale(force = false) {
  if (document.hidden) return;
  if (state.date !== todayStr()) return;          // 当日以外は変化しない
  if (!force && Date.now() - _lastLoad < REFRESH_MS) return;
  _lastLoad = Date.now();
  loadPage(state.page);
}

function startAutoRefresh() {
  if (_refreshTimer) clearInterval(_refreshTimer);
  _refreshTimer = setInterval(() => refreshIfStale(), 60 * 1000);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshIfStale();          // 戻ってきたら鮮度を確認
});

// ════════════════════════════════
// 初期化
// ════════════════════════════════
updateDateLabel();
loadBets();
startAutoRefresh();
