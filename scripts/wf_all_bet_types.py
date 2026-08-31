"""全賭式を払戻ベースで評価する（オッズ不要・全期間）。

なぜ払戻を使うか
--------------
拡連複と複勝は **odds テーブルに1件も無い**（収集していない）。だが
payouts には 2026-01-01 から全レースぶん入っている。

    kakurenfuku 110,550件 / 36,844レース（1レース3組）
    複勝         73,764件 / 36,844レース（1レース2組）

選び方がオッズに依存しないなら（＝モデルの確率が最大の1点）、
**払戻だけで「買っていたらいくら戻ったか」が計算できる。**
オッズ基準の分析は全通り揃う 6,189レースに限られていたが、
こちらは全期間 36,844レースを使える。

⚠️ 無作為の期待回収を払戻の平均から出すのは**間違い**（2026-08-30 に一度やった）:

    払戻ベース   単勝68.5% / 2連複60.7% / 3連単59.5%
    オッズベース 単勝73.6% / 2連複74.2% / 3連単74.8%   ← こちらが正しい

払戻の分布は裾が極端に重く、まれな大穴が18,000レース程度では標本に十分
現れない。そのため平均が過小評価になり、通り数が多い賭式ほどズレる。
`sum(1/確定オッズ)` の中央値から出す方が桁違いに安定する。

**モデルが選ぶのは本命なので、回収率そのものは裾の影響を受けず信頼できる。**
影響を受けるのは「無作為との差」だけ。ここでは実測できる賭式は
オッズ由来の値を使い、拡連複・複勝はオッズが無いので参考値なしとする。

ユーザーの想定する使い分け（2026-08-30）:
    固い          拡連複・単勝（＋複勝）
    それなりに勝負  2連複・3連複
    夢            3連単

使い方:
    python scripts/wf_all_bet_types.py
"""
from __future__ import annotations

import random
import sys
from collections import defaultdict
from itertools import combinations, permutations
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

from src.features.builder import build_features, FEATURE_COLS   # noqa: E402
from src.models import plackett_luce as pl                      # noqa: E402
from src.ingestion.database import get_engine, init_db          # noqa: E402
from src.utils.helpers import load_config                       # noqa: E402
from wf_bet_types import train_at, HORIZON                      # noqa: E402

CUTOFFS = ["2026-05-01", "2026-05-21", "2026-06-10",
           "2026-06-30", "2026-07-20", "2026-08-09"]
# 賭式 -> (通り数, 当たり組の数)
SPECS = {
    "tansho": (6, 1), "fukusho": (6, 2), "kakurenfuku": (15, 3),
    "nirenfuku": (15, 1), "sanrenfuku": (20, 1), "sanrentan": (120, 1),
}
TIER = {"tansho": "固い", "fukusho": "固い", "kakurenfuku": "固い",
        "nirenfuku": "勝負", "sanrenfuku": "勝負", "sanrentan": "夢"}
# 無作為に買ったときの期待回収。sum(1/確定オッズ) の中央値から算出（裾に強い）。
# 拡連複・複勝はオッズを収集していないので不明。
BASE = {"tansho": 73.6, "nirenfuku": 74.2, "sanrenfuku": 74.4, "sanrentan": 74.8}
JP = {"tansho": "単勝", "fukusho": "複勝", "kakurenfuku": "拡連複",
      "nirenfuku": "2連複", "sanrenfuku": "3連複", "sanrentan": "3連単"}
# payouts の bet_type。複勝だけ日本語のまま入っている（文字化けして見えるが実体は「複勝」）
# 2026-08-31 まで payouts に複勝だけ日本語で入っていたので橋渡しが要った。
# 発生源（official.BET_TYPE_MAP の登録漏れ）を直し、DBも移行したので空でよい。
# ⚠️ 残してあるのは、古い DB を相手に回すことがあるため。
PAYOUT_KEY: dict[str, str] = {}


def probs_for(exp_s, bt):
    """賭式ごとの全組合せ確率。表記は payouts と同じ並びにする。"""
    boats = sorted(exp_s)
    tot = sum(exp_s.values())
    if bt == "tansho":
        return {str(b): exp_s[b] / tot for b in boats}
    if bt == "fukusho":
        # P(a が2着以内) = P(a 1着) + Σ_b P(b 1着)·P(a 2着|b 1着)
        out = {}
        for a in boats:
            p = exp_s[a] / tot
            for b in boats:
                if b == a:
                    continue
                p += (exp_s[b] / tot) * (exp_s[a] / (tot - exp_s[b]))
            out[str(a)] = p
        return out
    if bt == "kakurenfuku":
        # P(a と b がともに3着以内) = Σ_c P({a,b,c} が1-3着)
        out = {}
        for a, b in combinations(boats, 2):
            out[f"{a}-{b}"] = sum(pl.joint_prob_sanrenfuku(exp_s, a, b, c)
                                  for c in boats if c not in (a, b))
        return out
    if bt == "nirenfuku":
        return {f"{a}-{b}": pl.joint_prob_nirenfuku(exp_s, a, b)
                for a, b in combinations(boats, 2)}
    if bt == "sanrenfuku":
        return {f"{a}-{b}-{c}": pl.joint_prob_sanrenfuku(exp_s, a, b, c)
                for a, b, c in combinations(boats, 3)}
    return {f"{a}-{b}-{c}": pl.joint_prob_sanrentan(exp_s, a, b, c)
            for a, b, c in permutations(boats, 3)}


def evaluate(model, d1, d2):
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    if df.empty:
        return []
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce").values.astype(float)
    X = np.where(np.isnan(X), model._medians, X)
    df = df.assign(score=model.predict(X))

    from sqlalchemy import text
    with get_engine().connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.bet_type,p.combination,p.payout FROM payouts p "
            "JOIN races r ON r.id=p.race_id WHERE r.race_date BETWEEN :d1 AND :d2"),
            {"d1": d1, "d2": d2}).fetchall()

    won = defaultdict(lambda: defaultdict(dict))
    for rid, bt, cb, p in pay:
        won[int(rid)][str(bt)][str(cb)] = (p or 0) / 100.0

    rows = []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        w = won.get(rid)
        if not w:
            continue
        scores = {int(r.boat_no): float(r.score) for r in grp.itertuples()}
        if len(scores) < 6:
            continue
        exp_s = pl.to_exp_scores(scores, temperature=temp)
        row, ok = {}, True
        for bt, (ncomb, nwin) in SPECS.items():
            key = PAYOUT_KEY.get(bt, bt)
            hits = w.get(key, {})
            if len(hits) != nwin:          # 中止・同着などで数が合わない回は使わない
                ok = False
                break
            p = probs_for(exp_s, bt)
            top = max(p, key=p.get)
            row[bt] = {"ret": hits.get(top, 0.0), "hit": int(top in hits)}
        if ok:
            rows.append(row)
    return rows


def boot(vals, T=1500):
    if len(vals) < 30:
        return None, None
    random.seed(0)
    v = sorted(sum(s) / len(s) * 100 for s in
               ([random.choice(vals) for _ in vals] for _ in range(T)))
    return v[int(.025 * T)], v[int(.975 * T)]


def report(label, rows):
    print(f"  {label}  {len(rows)}レース")
    print(f"    {'賭式':<8}{'層':<6}{'的中率':>8}{'回収率':>9}{'95%区間':>14}"
          f"{'無作為':>9}{'差':>8}")
    for bt in SPECS:
        r = [x[bt] for x in rows]
        n = len(r)
        if not n:
            continue
        hit = sum(x["hit"] for x in r) / n * 100
        roi = sum(x["ret"] for x in r) / n * 100
        base = BASE.get(bt)
        lo, hi = boot([x["ret"] for x in r])
        ci = f"[{lo:.0f}〜{hi:.0f}]" if lo is not None else ""
        star = " ★" if lo is not None and lo > 100 else ""
        bs = f"{base:8.1f}%{roi - base:+7.1f}pt" if base else f"{'—':>8}{'—':>9}"
        print(f"    {JP[bt]:<8}{TIER[bt]:<6}{hit:7.2f}%{roi:8.1f}%{ci:>14}{bs}{star}")


def main():
    init_db(load_config())
    seed = load_config()["model"].get("random_state", 42)
    print("全賭式の比較（払戻ベース・同じレース・同じモデル）")
    print("★ = 95%区間の下限が100%超（＝儲かると言える）")
    print()
    per = {}
    for cu in CUTOFFS:
        d1 = (pd.Timestamp(cu) + pd.Timedelta(days=1)).date().isoformat()
        d2 = (pd.Timestamp(cu) + pd.Timedelta(days=HORIZON)).date().isoformat()
        m, ntr = train_at(cu, seed)
        if m is None:
            continue
        per[cu] = evaluate(m, d1, d2)
        print(f"打ち切り {cu}（訓練 {ntr}レース）→ 予測 {d1}〜{d2}")
        report("この窓", per[cu])
        print()
    half = len(CUTOFFS) // 2
    for name, cus in (("窓A（前半）", CUTOFFS[:half]), ("窓B（後半）", CUTOFFS[half:])):
        rows = [x for c in cus for x in per.get(c, [])]
        if rows:
            print(f"=== {name} {cus} ===")
            report("合計", rows)
            print()
    print("判定: **両窓とも★** で初めて採用に値する。")


if __name__ == "__main__":
    main()
