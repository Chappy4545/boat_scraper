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
function evColor(ev) {
  if (ev >= 1.5) return "#ff7043";   // 高EV：オレンジ
  if (ev >= 1.3) return "#ffd54f";   // 中高EV：ゴールド
  return "#90caf9";                   // 標準EV：水色
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

    const refreshEl = document.getElementById("odds-refresh-time");
    if (meta && meta.last_refreshed) {
      const t = new Date(meta.last_refreshed);
      const hm = t.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
      const src = meta.source === "github_actions" ? "自動更新" : "手動更新";
      refreshEl.textContent = `オッズ最終更新: ${hm} (${src})`;
    } else {
      refreshEl.textContent = "";
    }

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
    document.getElementById("yesterday-result").innerHTML = "";
    container.innerHTML = e.message === "404"
      ? '<div class="empty">この日のデータがありません</div>'
      : `<div class="empty">取得失敗 (${e.message})</div>`;
  }
}

function renderYesterdayResult(yDate, bets) {
  const el = document.getElementById("yesterday-result");
  const settled = bets.filter(b => b.is_hit !== null && b.is_hit !== undefined);
  if (!settled.length) { el.innerHTML = ""; return; }

  const hits     = settled.filter(b => b.is_hit === true);
  const invested = settled.reduce((s, b) => s + (b.recommended_amount || 0), 0);
  const returned = hits.reduce((s, b) => s + (b.actual_payout || 0), 0);
  const roi      = invested > 0 ? returned / invested : 0;
  const roiCls   = roi >= 1 ? "val-good" : "val-bad";
  const hitRate  = settled.length > 0 ? hits.length / settled.length : 0;

  const BET_LABEL = { sanrentan:"3連単", sanrenfuku:"3連複", nirentan:"2連単", nirenfuku:"2連複" };

  const hitsHtml = hits.length === 0 ? "" : `
    <div class="yesterday-hits">
      ${hits.map(b => `
        <div class="yesterday-hit-row">
          <span class="yesterday-hit-place">${b.stadium_name || ""} R${b.race_no}</span>
          <span class="yesterday-hit-type">${BET_LABEL[b.bet_type] || b.bet_type}</span>
          <span class="yesterday-hit-combo">${b.combination}</span>
          <span class="yesterday-hit-payout">¥${(b.actual_payout || 0).toLocaleString()}</span>
        </div>`).join("")}
    </div>`;

  el.innerHTML = `
    <div class="yesterday-card" id="yesterday-card-click">
      <div class="yesterday-card__head">
        <span class="yesterday-card__label">前日実績 <span class="yesterday-card__date">${fmtDate(yDate)}</span></span>
        <span class="yesterday-card__link">詳細 ›</span>
      </div>
      <div class="yesterday-card__stats">
        <div class="yesterday-stat">
          <div class="yesterday-stat__val">${hits.length}<span class="yesterday-stat__denom">/${settled.length}</span></div>
          <div class="yesterday-stat__label">的中</div>
        </div>
        <div class="yesterday-stat">
          <div class="yesterday-stat__val">${(hitRate * 100).toFixed(0)}<span class="yesterday-stat__denom">%</span></div>
          <div class="yesterday-stat__label">的中率</div>
        </div>
        <div class="yesterday-stat">
          <div class="yesterday-stat__val ${roiCls}">¥${returned.toLocaleString()}</div>
          <div class="yesterday-stat__label">回収</div>
        </div>
        <div class="yesterday-stat">
          <div class="yesterday-stat__val ${roiCls}">${(roi * 100).toFixed(0)}<span class="yesterday-stat__denom">%</span></div>
          <div class="yesterday-stat__label">ROI</div>
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
    <span class="ev-tier" style="color:#90caf9">1.20〜1.29　標準</span>
    <span class="ev-tier" style="color:#ffd54f">1.30〜1.49　高EV</span>
    <span class="ev-tier" style="color:#ff7043">1.50〜　　　超高EV</span>
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
  if (state.betsSort === "ev") {
    filtered = [...filtered].sort((a, b) => (b.expected_value || 0) - (a.expected_value || 0));
  } else {
    filtered = [...filtered].sort((a, b) => {
      if (a.stadium_name !== b.stadium_name) return a.stadium_name.localeCompare(b.stadium_name, "ja");
      if (a.race_no !== b.race_no) return a.race_no - b.race_no;
      return (b.expected_value || 0) - (a.expected_value || 0);
    });
  }

  // ── サマリー ──
  const totalAmt = filtered.reduce((s, b) => s + (b.recommended_amount || 0), 0);
  const maxEv = filtered.length ? Math.max(...filtered.map(b => b.expected_value || 0)) : 0;
  const highCount = filtered.filter(b => (b.expected_value || 0) >= 1.3).length;
  document.getElementById("bets-summary").innerHTML = filtered.length ? `
    <div class="bets-summary">
      <span>推奨 <strong>${filtered.length}</strong> 件</span>
      ${highCount > 0 ? `<span>高EV <strong style="color:#ffd54f">${highCount}</strong> 件</span>` : ""}
      <span>合計 <strong>¥${totalAmt.toLocaleString()}</strong></span>
      <span>最高EV <strong style="color:${evColor(maxEv)}">${maxEv.toFixed(2)}</strong></span>
    </div>` : "";

  // ── カード描画（EV順のときはティア区切りを挿入）──
  const container = document.getElementById("bet-list");
  if (!filtered.length) {
    container.innerHTML = '<div class="empty">該当する買い目がありません</div>';
    return;
  }

  let html = "";
  let lastTier = null;
  filtered.forEach((b, i) => {
    if (state.betsSort === "ev") {
      const ev = b.expected_value || 0;
      const tier = ev >= 1.5 ? "超高EV（1.50+）" : ev >= 1.3 ? "高EV（1.30〜1.49）" : "標準（1.20〜1.29）";
      const tierColor = ev >= 1.5 ? "#ff7043" : ev >= 1.3 ? "#ffd54f" : "#90caf9";
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
  const hitLabel = b.is_hit === true
    ? `<span style="color:var(--green)">✓ 的中 +¥${(b.actual_payout||0).toLocaleString()}</span>`
    : b.is_hit === false ? `<span style="color:var(--red)">✗ 外れ</span>` : "";
  const raceTypeShort = b.race_type
    ? b.race_type.replace("レディース/", "L/").replace("予選", "予").replace("準優勝戦", "準優").replace("優勝戦", "優")
    : "";

  return `
    <div class="bet-card ${hitCls}" style="cursor:pointer;border-left-color:${color}">
      <div class="bet-card__head">
        <div class="bet-card__race">
          ${gradeBadge(b.grade)}
          ${categoryBadges(b.race_type, b.is_night)}
          <span>${b.stadium_name} R${b.race_no}</span>
          ${raceTypeShort ? `<span class="race-type-label">${raceTypeShort}</span>` : ""}
          ${b.closing_time ? `<span>⏱${b.closing_time}</span>` : ""}
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
        <span>確率 ${((b.model_prob||0)*100).toFixed(1)}% / オッズ ${(b.odds||0).toFixed(1)}x</span>
        ${hitLabel}
      </div>
    </div>`;
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

  return `
    <div class="race-card${betCount > 0 ? " has-bets" : ""}" style="cursor:pointer">
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
      ${betBadge ? `<div style="margin-top:.45rem">${betBadge}</div>` : ""}
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
async function loadPerf() {
  const container = document.getElementById("perf-content");
  container.innerHTML = '<div class="empty">読込中…</div>';
  try {
    const data = await api("data/performance.json").catch(() => null);
    const perf = data;
    const bt = data?.backtest ?? null;
    let html = "";
    if (perf && perf.settled_bets > 0) {
      const roi = perf.roi ?? 0;
      html += `
        <p class="info-card__label" style="margin-bottom:.5rem;">実際の買い目実績</p>
        <div class="stats-grid">
          <div class="stat-card"><div class="stat-card__label">回収率</div>
            <div class="stat-card__value ${roi>=1?"val-good":"val-bad"}">${(roi*100).toFixed(1)}%</div></div>
          <div class="stat-card"><div class="stat-card__label">的中率</div>
            <div class="stat-card__value val-gold">${((perf.hit_rate||0)*100).toFixed(1)}%</div></div>
          <div class="stat-card"><div class="stat-card__label">的中</div>
            <div class="stat-card__value">${perf.hits}/${perf.settled_bets}</div></div>
          <div class="stat-card"><div class="stat-card__label">投資合計</div>
            <div class="stat-card__value">¥${(perf.invested||0).toLocaleString()}</div></div>
          <div class="stat-card"><div class="stat-card__label">回収合計</div>
            <div class="stat-card__value ${roi>=1?"val-good":"val-bad"}">¥${(perf.returned||0).toLocaleString()}</div></div>
        </div>`;
    } else {
      html += `<div class="info-card">
        <div class="info-card__label">実績</div>
        <div class="info-card__value val-muted">まだ買い目実績なし</div>
        <div class="info-card__sub">予測実行後、結果が記録されると表示されます</div>
      </div>`;
    }
    if (bt) {
      const roi = bt.roi ?? 0;
      html += `
        <p class="info-card__label" style="margin:.9rem 0 .5rem;">バックテスト参考値（${bt.date_start} 〜 ${bt.date_end}）</p>
        <div class="stats-grid">
          <div class="stat-card"><div class="stat-card__label">回収率</div>
            <div class="stat-card__value ${roi>=1?"val-good":"val-bad"}">${(roi*100).toFixed(1)}%</div></div>
          <div class="stat-card"><div class="stat-card__label">的中率</div>
            <div class="stat-card__value val-gold">${((bt.hit_rate||0)*100).toFixed(1)}%</div></div>
          <div class="stat-card"><div class="stat-card__label">購入レース</div>
            <div class="stat-card__value">${(bt.bet_races||0).toLocaleString()}</div></div>
          <div class="stat-card"><div class="stat-card__label">最大DD</div>
            <div class="stat-card__value val-bad">${((bt.max_drawdown||0)*100).toFixed(1)}%</div></div>
          <div class="stat-card"><div class="stat-card__label">平均オッズ</div>
            <div class="stat-card__value">${(bt.avg_odds||0).toFixed(1)}x</div></div>
        </div>`;
    }

    // 日別実績テーブル
    const daily = data?.daily ?? [];
    if (daily.length > 0) {
      const rows = daily.map(d => {
        const roi = d.roi ?? 0;
        const hitRate = d.bets > 0 ? d.hits / d.bets : 0;
        const roiCls = roi >= 1 ? "val-good" : roi > 0 ? "" : "val-bad";
        return `<tr>
          <td>${d.date}</td>
          <td style="text-align:center">${d.bets}</td>
          <td style="text-align:center">${d.hits}</td>
          <td style="text-align:center;color:var(--gold)">${(hitRate*100).toFixed(0)}%</td>
          <td style="text-align:right">¥${(d.invested||0).toLocaleString()}</td>
          <td style="text-align:right">¥${(d.returned||0).toLocaleString()}</td>
          <td style="text-align:right;font-weight:600" class="${roiCls}">${roi ? (roi*100).toFixed(0)+"%" : "—"}</td>
        </tr>`;
      }).join("");
      html += `
        <p class="info-card__label" style="margin:.9rem 0 .5rem;">日別実績</p>
        <div class="daily-table-wrap">
          <table class="daily-table">
            <thead><tr>
              <th>日付</th><th>件数</th><th>的中</th><th>的中率</th>
              <th>投資</th><th>回収</th><th>ROI</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
    }

    container.innerHTML = html || '<div class="empty">データなし</div>';
  } catch (e) {
    container.innerHTML = `<div class="empty">取得失敗 (${e.message})</div>`;
  }
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
  if (page === "bets")     loadBets();
  if (page === "races")    loadRaces();
  if (page === "perf")     loadPerf();
  if (page === "settings") loadSettings();
  if (page === "info")     renderInfoPage();
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
// 初期化
// ════════════════════════════════
updateDateLabel();
loadBets();
