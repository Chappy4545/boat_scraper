"""艇の勝率 → 2連複確率 の変換を、市場を教師にして学習する。

なぜ:
  Plackett-Luce 変換は、市場自身の単勝確率に当てても 5.3% 精度を落とす
  （scripts/pl_cost.py で測定・1,413レース）。モデルの良し悪しと無関係に、
  変換の段階で情報を捨てている。艇の評価がどれだけ良くても、この後段で
  必ず失う。ここを直すのが一番大きい。

なぜ市場を教師にするか:
  着順を教師にすると1レースに正解が1つしかなく学習が遅い。市場の2連複
  オッズは「その組が来る確率」の非常に良い推定で、1レースにつき15個の
  教師信号が得られる。市場に追いつくことが目的なら、市場を写すのが最短。

学習するもの:
  target = log(市場の2連複確率 / PLの2連複確率)   ← PLの誤りそのもの
  特徴  = 艇番の組・両艇の勝率と順位・勝率差・枠の距離 など

⚠️ 訓練と評価の期間を必ず分ける。

使い方:
    python scripts/learn_pair_transform.py <訓練from> <訓練to> <評価from> <評価to>
"""
from __future__ import annotations

import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

# 途中経過を見えるようにする。リダイレクトすると Python は標準出力を
# ブロックバッファするので、終わるまで1行も出ない（実測: 10分間0バイト）。
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import plackett_luce as pl        # noqa: E402
from src.ingestion.database import get_engine     # noqa: E402


def clip(p):
    return min(max(p, 1e-9), 1 - 1e-9)


def load(d1, d2):
    """レースごとに、単勝市場・PL・2連複市場・着順をまとめて返す。"""
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
        tt = sum(1.0 / v for v in t.values())
        win = {int(k): (1.0 / v) / tt for k, v in t.items()}
        exp_s = pl.to_exp_scores({k: math.log(max(v, 1e-9)) for k, v in win.items()}, 1.0)
        nt = sum(1.0 / v for v in nf.values())
        pairs = {}
        for cb, v in nf.items():
            a, b = sorted(int(x) for x in cb.split("-"))
            pairs[(a, b)] = {
                "pl": pl.joint_prob_nirenfuku(exp_s, a, b),
                "mkt": (1.0 / v) / nt,
                "hit": set(cb.split("-")) == {fin[1], fin[2]},
            }
        if len(pairs) == 15:
            out.append({"win": win, "pairs": pairs})
    return out


def featurize(race):
    """1レース15行の特徴量と、PL確率を返す。"""
    win = race["win"]
    rank = {k: i for i, k in enumerate(sorted(win, key=lambda x: -win[x]))}
    rows, keys, plp = [], [], []
    for (a, b), v in sorted(race["pairs"].items()):
        wa, wb = win.get(a, 0.0), win.get(b, 0.0)
        rows.append([
            a, b, abs(a - b),                       # 枠と距離
            wa, wb, wa + wb, abs(wa - wb),          # 勝率
            rank[a], rank[b], abs(rank[a] - rank[b]),
            math.log(clip(v["pl"])),                # PLの見立て
            max(win.values()),                      # レースの堅さ
        ])
        keys.append((a, b))
        plp.append(v["pl"])
    return np.array(rows, dtype=float), keys, np.array(plp)


def gap_vs_market(races, probs_of):
    d = mk = 0.0
    for r in races:
        p = probs_of(r)
        for k, v in r["pairs"].items():
            y = v["hit"]
            lp = -(math.log(clip(p[k])) if y else math.log(1 - clip(p[k])))
            lq = -(math.log(clip(v["mkt"])) if y else math.log(1 - clip(v["mkt"])))
            d += lq - lp
            mk += lq
    return d / mk * 100


def boot(races, probs_of, T=1200):
    random.seed(0)
    v = sorted(gap_vs_market([random.choice(races) for _ in races], probs_of)
               for _ in range(T))
    return v[int(.025 * T)], v[int(.975 * T)]


def main():
    import lightgbm as lgb

    t1, t2, e1, e2 = sys.argv[1:5]
    tr, te = load(t1, t2), load(e1, e2)
    print("訓練 %s〜%s: %d レース / 評価 %s〜%s: %d レース"
          % (t1, t2, len(tr), e1, e2, len(te)))
    if len(tr) < 300 or len(te) < 100:
        raise SystemExit("データ不足")

    X, Y = [], []
    for r in tr:
        f, keys, plp = featurize(r)
        X.append(f)
        Y.append([math.log(clip(r["pairs"][k]["mkt"]) / clip(p))
                  for k, p in zip(keys, plp)])
    X = np.vstack(X)
    Y = np.concatenate(Y)
    print("学習データ %d行 %d特徴" % X.shape)

    model = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.05,
                              num_leaves=31, min_child_samples=50, verbose=-1)
    model.fit(X, Y)

    def learned(race):
        f, keys, plp = featurize(race)
        adj = plp * np.exp(model.predict(f))
        adj = adj / adj.sum()
        return dict(zip(keys, adj))

    def plain(race):
        return {k: v["pl"] for k, v in race["pairs"].items()}

    print()
    print("=== 評価期間：市場との差（正なら市場より良い）===")
    a = gap_vs_market(te, plain)
    la, ha = boot(te, plain)
    print("  PLそのまま   %+.2f%%  [%+.2f%%, %+.2f%%]" % (a, la, ha))
    b = gap_vs_market(te, learned)
    lb, hb = boot(te, learned)
    print("  学習した変換 %+.2f%%  [%+.2f%%, %+.2f%%]" % (b, lb, hb))
    print()
    print("  取り戻した分 %+.2f ポイント（PL変換の損失 約5.3が上限）" % (b - a))

    imp = sorted(zip(["a", "b", "|a-b|", "win_a", "win_b", "win_a+b", "|win差|",
                      "rank_a", "rank_b", "|rank差|", "log(PL)", "最高勝率"],
                     model.feature_importances_), key=lambda x: -x[1])
    print()
    print("=== 効いた特徴 ===")
    for k, v in imp[:6]:
        print("  %-10s %d" % (k, v))


if __name__ == "__main__":
    main()
