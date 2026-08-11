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
    bets:  { stadium: null },
    races: { stadium: null, grade: null },
  },
  betsSort: "ev",       // "ev" | "race"
  evInfoOpen: false,
  _racesCache: [],
  _betsCache:  [],
};

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
// 実測(未見データ2期間)の EV帯別 回収率に合わせた段階。
//   1.2〜1.5 → 100〜120% / 1.5〜2.0 → 126〜138% / 2.0〜3.0 → 142〜174% / 3.0〜 → 239〜280%
// 旧実装は 1.5 以上を最上位にしていたため、ほぼ全ての買い目が最も派手な色になり
// 「全部目立つ＝何も目立たない」状態だった。本当に良いものだけを目立たせる。
function evColor(ev) {
  if (ev >= 3.0) return "#ff7043";   // 実測で飛び抜けて良い帯
  if (ev >= 2.0) return "#ffb74d";
  if (ev >= 1.5) return "#ffd54f";
  return "#90a4ae";                   // 標準帯は退かせる（muted）
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
  return { sanrentan:"3連単", sanrenfuku:"3連複", nirentan:"2連単", nirenfuku:"2連複" }[t] ?? t;
}

// actual_payout は「100円あたりの払戻額」（オッズ3.6倍 → 360）。
// 実際の払戻は 賭け金 × payout / 100。
// 以前はこの値をそのまま金額として表示・集計しており、
// 回収額が実際の 1/5 程度に見えていた（賭け金500円なら5倍の誤差）。
function payoutOf(bet) {
  if (!bet || bet.actual_payout == null) return 0;
  return Math.round((bet.recommended_amount || 0) * bet.actual_payout / 100);
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
function buildFilterBar(items, getKey, activeVal, onSelect, allLabel = "すべて") {
  const counts = {};
  items.forEach(item => {
    const k = getKey(item) || "—";
    counts[k] = (counts[k] || 0) + 1;
  });
  const keys = Object.keys(counts).sort();

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
    const [bets, races, yBets, meta] = await Promise.all([
      api(`data/bets_${state.date}.json`),
      api(`data/races_${state.date}.json`).catch(() => []),
      isToday ? api(`data/bets_${yDate}.json`).catch(() => []) : Promise.resolve([]),
      isToday ? api(`data/meta.json`).catch(() => null) : Promise.resolve(null),
    ]);
    state._betsCache = bets;
    state._racesCache = races;

    // 更新時刻は独立行にせず day-panel の中へ（スマホの縦を1行ぶん節約）
    let refreshText = "";
    if (meta && meta.last_refreshed) {
      const t = new Date(meta.last_refreshed);
      const hm = t.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
      const src = meta.source === "github_actions" ? "自動更新" : "手動更新";
      refreshText = `オッズ最終更新 ${hm}（${src}）`;
    }
    document.getElementById("odds-refresh-time").textContent = "";

    renderDayPanel(state.date, bets, refreshText);
    renderYesterdayResult(yDate, yBets);

    if (!bets.length) {
      document.getElementById("bets-filter-area").innerHTML = "";
      document.getElementById("bets-summary").innerHTML = "";
      container.innerHTML = '<div class="empty">この日の推奨買い目はありません</div>';
      return;
    }

    renderBets();
  } catch (e) {
    document.getElementById("bets-filter-area").innerHTML = "";
    document.getElementById("bets-summary").innerHTML = "";
    document.getElementById("day-panel").innerHTML = "";
    document.getElementById("yesterday-result").innerHTML = "";
    container.innerHTML = e.message === "404"
      ? '<div class="empty">この日のデータがありません</div>'
      : `<div class="empty">取得失敗 (${e.message})</div>`;
  }
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
        <div class="day-hero__label">${good ? "▲ 損益（プラス）" : "▼ 損益（マイナス）"}</div>
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
          <div class="stat__lab">投資</div>
        </div>
      </div>
      ${sub}
    </section>`;
}

function renderYesterdayResult(yDate, bets) {
  const el = document.getElementById("yesterday-result");
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
  <p class="ev-info-desc">モデルが「当たりやすい」と判断した組み合わせのオッズが高いほどEVが上がります。EV&gt;1.0で期待値プラス、このシステムはEV≥1.20のみ推奨します。</p>
  <div class="ev-info-tiers">
    <span class="ev-tier" style="color:#90a4ae">1.2〜1.5　実測 100〜120%</span>
    <span class="ev-tier" style="color:#ffd54f">1.5〜2.0　実測 126〜138%</span>
    <span class="ev-tier" style="color:#ffb74d">2.0〜3.0　実測 142〜174%</span>
    <span class="ev-tier" style="color:#ff7043">3.0〜　　 実測 239〜280%</span>
  </div>
</div>`;

function renderBets() {
  const bets = state._betsCache;
  const f = state.filters.bets;

  // ── フィルター＆ソートエリア ──
  const filterArea = document.getElementById("bets-filter-area");
  filterArea.innerHTML = "";

  // EV説明トグル
  const infoRow = document.createElement("div");
  infoRow.className = "bets-toolbar";
  infoRow.innerHTML = `
    <button class="ev-info-btn" id="ev-info-toggle" title="EVとは？">
      <span>EVとは？</span> <span id="ev-info-arrow">${state.evInfoOpen ? "▲" : "▼"}</span>
    </button>
    <div class="sort-toggle">
      <button class="sort-btn${state.betsSort === "ev" ? " active" : ""}" data-sort="ev">EV順</button>
      <button class="sort-btn${state.betsSort === "race" ? " active" : ""}" data-sort="race">開催順</button>
    </div>`;
  filterArea.appendChild(infoRow);

  // EV説明パネル
  const infoPanel = document.createElement("div");
  infoPanel.id = "ev-info-panel";
  infoPanel.innerHTML = state.evInfoOpen ? EV_EXPLAIN_HTML : "";
  filterArea.appendChild(infoPanel);

  // 場別フィルター
  filterArea.appendChild(
    buildFilterBar(bets, b => b.stadium_name, f.stadium, val => {
      state.filters.bets.stadium = val;
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

  // ── フィルター適用 ──
  let filtered = f.stadium ? bets.filter(b => b.stadium_name === f.stadium) : bets;

  // ── ソート ──
  const bySort = (a, b) => {
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
  const totalAmt = filtered.reduce((s, b) => s + (b.recommended_amount || 0), 0);
  const isFiltered = filtered.length !== bets.length;
  document.getElementById("bets-summary").innerHTML = isFiltered && filtered.length ? `
    <div class="bets-summary">
      <span>絞り込み <strong>${filtered.length}</strong>/${bets.length} 件</span>
      <span>投資 <strong>¥${totalAmt.toLocaleString()}</strong></span>
    </div>` : "";

  // ── カード描画（EV順のときはティア区切りを挿入）──
  const container = document.getElementById("bet-list");
  if (!filtered.length) {
    container.innerHTML = '<div class="empty">該当する買い目がありません</div>';
    return;
  }

  let html = "";
  let lastTier = null;
  let section = null;
  filtered.forEach((b, i) => {
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
      // 区切りは実測の回収率帯に対応させる（旧: 1.3/1.5 の2段階）
      const tier =
        ev >= 3.0 ? "EV 3.0以上　実測 回収率 239〜280%" :
        ev >= 2.0 ? "EV 2.0〜3.0　実測 142〜174%" :
        ev >= 1.5 ? "EV 1.5〜2.0　実測 126〜138%" :
                    "EV 1.2〜1.5　実測 100〜120%";
      const tierColor = evColor(ev);
      if (tier !== lastTier) {
        html += `<div class="ev-tier-divider" style="color:${tierColor}">${tier}</div>`;
        lastTier = tier;
      }
    }
    html += buildBetCard(b, i);
  });
  container.innerHTML = html;
  container.querySelectorAll(".bet-card").forEach((el, i) => {
    el.addEventListener("click", () => openRaceModal(filtered[i].race_id,
      `${filtered[i].stadium_name} R${filtered[i].race_no}`));
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
        <span class="bet-card__ev" style="color:${color}">EV ${ev.toFixed(2)}</span>
      </div>
      <div class="bet-card__body">
        <div class="bet-card__combo">
          <span class="bet-type-label">${betTypeLabel(b.bet_type)}</span>
          ${comboSpans(b.combination)}
        </div>
        <span class="bet-card__amount">¥${(b.recommended_amount||0).toLocaleString()}</span>
      </div>
      <div class="bet-card__foot">
        <span class="bet-card__stats">
          確率 <b>${((b.model_prob||0)*100).toFixed(1)}%</b>
          <span class="dot-sep">·</span>
          オッズ <b>${(b.odds||0).toFixed(1)}x</b>${oddsMark(b.odds)}
        </span>
        ${hitLabel}
      </div>
      ${orderHtml ? `<div class="bet-card__result">${orderHtml}</div>` : ""}
    </div>`;
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
function openRaceModal(raceId, title) {
  const race    = state._racesCache.find(r => r.id === raceId);
  const entries = race?.entries ?? [];
  const preds   = race?.predictions ?? [];
  const raceBets = state._betsCache.filter(b => b.race_id === raceId);
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

    const betsSection = raceBets.length ? `
      <div style="margin-top:1rem;">
        <p style="font-size:.78rem;color:var(--muted);margin-bottom:.4rem;">推奨買い目</p>
        ${raceBets.map(b => `
          <div style="display:flex;justify-content:space-between;align-items:center;
                      padding:.35rem 0;border-bottom:1px solid var(--surface2);font-size:.85rem;">
            <span>${betTypeLabel(b.bet_type)} ${comboSpans(b.combination)}</span>
            <span style="color:var(--gold);font-weight:700;">EV ${(b.expected_value||0).toFixed(2)}</span>
            <span style="color:var(--green);">¥${(b.recommended_amount||0).toLocaleString()}</span>
          </div>`).join("")}
      </div>` : "";

  openModal(`
    <h3 style="font-weight:700;margin-bottom:.75rem;">${title}</h3>
    <table class="entry-table">
      <thead><tr>
        <th>枠</th><th style="text-align:left">選手</th><th>級</th>
        <th>全勝率</th><th>M2連</th><th>1着%</th>
      </tr></thead>
      <tbody>${entryRows}</tbody>
    </table>
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
            <span class="day-panel__title">${W[_perfWindow]}の損益</span>
            <span class="chip">${t.bets} 買い目</span>
          </div>
          <div class="day-hero">
            <div class="day-hero__value ${good ? "is-good" : "is-bad"}">${good ? "+" : "−"}¥${Math.abs(Math.round(t.profit || 0)).toLocaleString()}</div>
            <div class="day-hero__label">${good ? "▲ 損益（プラス）" : "▼ 損益（マイナス）"}</div>
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
