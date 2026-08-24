"""PL変換の損失を、どこまで安く取り戻せるかを試す。

pl_cost.py で「PL変換だけで 5.3% 失う」ことが分かった（市場自身の単勝を
PLに通して、市場の2連複と比べた測定。モデルと無関係）。

ここでは一番安い補正を試す:
  艇番の組（1-2, 1-3, ... 5-6 の15通り）ごとに、実際の出現頻度が
  PL の予測とどれだけずれているかを学習し、掛けて正規化するだけ。
  パラメータ15個。PLの誤りが「どの枠の組か」で説明できるなら効くはず。

⚠️ 訓練期間と評価期間を必ず分ける。同じ期間で係数を作って同じ期間で
   測れば必ず良く出る。

使い方:
    python scripts/pl_fix_probe.py <訓練from> <訓練to> <評価from> <評価to>
"""
from __future__ import annotations

import math
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import plackett_luce as pl        # noqa: E402
from src.ingestion.database import get_engine     # noqa: E402


def clip(p):
    return min(max(p, 1e-9), 1 - 1e-9)


def load(d1, d2):
    """[(レースID, {組: (pl確率, 市場確率, 的中)})] を返す。"""
    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": ["tansho", "nirenfuku"]}
    with get_engine().connect() as conn:
        od = conn.execute(text(
            "SELECT o.race_id,o.bet_type,o.combination,o.odds FROM odds o "
            "JOIN races r ON r.id=o.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND o.is_final=1 AND o.odds>0 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no,rr.arrival_order FROM race_results rr "
            "JOIN races r ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order IS NOT NULL"
        ), prm).fetchall()
    odds = defaultdict(lambda: defaultdict(dict))
    for rid, bt, cb, o in od:
        odds[int(rid)][bt][str(cb)] = float(o)
    order = defaultdict(dict)
    for rid, bn, ao in res:
        order[int(rid)][int(ao)] = str(bn)

    out = []
    for rid, o in odds.items():
        t, nf = o.get("tansho", {}), o.get("nirenfuku", {})
        fin = order.get(rid, {})
        if len(t) != 6 or len(nf) != 15 or 1 not in fin or 2 not in fin:
            continue
        top2 = {fin[1], fin[2]}
        tt = sum(1.0 / v for v in t.values())
        q_win = {int(k): (1.0 / v) / tt for k, v in t.items()}
        exp_s = pl.to_exp_scores({k: math.log(max(v, 1e-9)) for k, v in q_win.items()}, 1.0)
        nt = sum(1.0 / v for v in nf.values())
        row = {}
        for cb, v in nf.items():
            a, b = (int(x) for x in cb.split("-"))
            row[tuple(sorted((a, b)))] = (
                pl.joint_prob_nirenfuku(exp_s, a, b),
                (1.0 / v) / nt,
                set(cb.split("-")) == top2,
            )
        if len(row) == 15:
            out.append(row)
    return out


def fit_correction(races):
    """組ごとに 実際の出現率 ÷ PLの平均予測 を係数にする。"""
    hit = defaultdict(float)
    pred = defaultdict(float)
    for r in races:
        for k, (p, _q, y) in r.items():
            pred[k] += p
            hit[k] += 1.0 if y else 0.0
    return {k: (hit[k] / pred[k] if pred[k] > 0 else 1.0) for k in pred}


def apply_corr(race, corr):
    adj = {k: v[0] * corr.get(k, 1.0) for k, v in race.items()}
    s = sum(adj.values())
    return {k: (v / s if s > 0 else v) for k, v in adj.items()}


def gap(races, get_p):
    """市場に対する対数損失差(%)。正ならこちらの勝ち。"""
    d = mk = 0.0
    n = 0
    for r in races:
        pr = get_p(r)
        for k, (_p, q, y) in r.items():
            p = pr[k]
            lp = -(math.log(clip(p)) if y else math.log(1 - clip(p)))
            lq = -(math.log(clip(q)) if y else math.log(1 - clip(q)))
            d += lq - lp
            mk += lq
            n += 1
    return d / mk * 100


def boot(races, get_p, T=1500):
    random.seed(0)
    v = sorted(gap([random.choice(races) for _ in races], get_p) for _ in range(T))
    return v[int(.025 * T)], v[int(.975 * T)]


def main():
    t1, t2, e1, e2 = sys.argv[1:5]
    tr = load(t1, t2)
    te = load(e1, e2)
    print("訓練 %s〜%s: %d レース / 評価 %s〜%s: %d レース"
          % (t1, t2, len(tr), e1, e2, len(te)))
    if len(tr) < 200 or len(te) < 100:
        raise SystemExit("データ不足")

    corr = fit_correction(tr)
    print()
    print("=== 学習した補正係数（1.0より大＝PLが過小評価している組）===")
    for k in sorted(corr, key=lambda x: -corr[x])[:5]:
        print("  %d-%d  ×%.3f" % (k[0], k[1], corr[k]))
    print("  ...")
    for k in sorted(corr, key=lambda x: corr[x])[:3]:
        print("  %d-%d  ×%.3f" % (k[0], k[1], corr[k]))

    print()
    print("=== 評価期間での市場との差 ===")
    raw = gap(te, lambda r: {k: v[0] for k, v in r.items()})
    lo, hi = boot(te, lambda r: {k: v[0] for k, v in r.items()})
    print("  PLそのまま     %+.2f%%  [%+.2f%%, %+.2f%%]" % (raw, lo, hi))
    fix = gap(te, lambda r: apply_corr(r, corr))
    lo2, hi2 = boot(te, lambda r: apply_corr(r, corr))
    print("  15個の補正あり %+.2f%%  [%+.2f%%, %+.2f%%]" % (fix, lo2, hi2))
    print()
    print("  取り戻した分 %+.2f ポイント" % (fix - raw))
    print("  （PL変換の損失は約5.3ポイント。ここが上限）")


if __name__ == "__main__":
    main()
