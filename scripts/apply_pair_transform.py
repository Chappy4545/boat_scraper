"""学習した変換を「自分たちのモデル」に当てて、効くかを確かめる。

learn_pair_transform.py は市場の単勝を入力にして +3.58pt を取り戻した。
だが本番で入力になるのはモデルの艇スコアで、分布が違う（モデルは
p>=0.40 帯で過信するなど歪みがある）。市場で学んだ補正がそのまま
効くとは限らないので、実際に当てて測る。

    A: モデルの艇スコア → PL          （現行）
    B: モデルの艇スコア → PL → 学習補正（提案）
  どちらも同じレース・同じ着順で、市場と比べる。

⚠️ モデルは評価期間を見ていないものを渡すこと（本番モデルは弾く）。
⚠️ 補正の訓練期間と評価期間も分けること。

2026-08-24 の結果: 市場の確率で学んだ補正はモデルには移らなかった。
    窓1 8/22〜23（  287レース） +2.27pt
    窓2 8/13〜21（1,281レース） +0.12pt   ← 再現せず
モデルの艇確率は市場とは分布が違う（p>=0.40帯で過信するなど）ため、
補正の効き所がずれる。--train-on-model で**モデルの確率を入力にして**
補正を学習し直せる（教師は市場の2連複確率のまま）。

使い方:
    python scripts/apply_pair_transform.py <model.joblib> \
        <補正の訓練from> <補正の訓練to> <評価from> <評価to> [--train-on-model]
"""
from __future__ import annotations

import logging
import math
import random
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features.builder import (            # noqa: E402
    build_features, FEATURE_COLS, EXTRA_FEATURE_COLS,
)
from src.models import plackett_luce as pl    # noqa: E402
from src.ingestion.database import get_engine  # noqa: E402
from src.utils.helpers import load_config      # noqa: E402
from learn_pair_transform import (             # noqa: E402
    load as load_market, featurize, clip,
)

BASE_COLS = [c for c in FEATURE_COLS if c not in EXTRA_FEATURE_COLS]
PROD = "data/processed/models/ranker_lightgbm.joblib"


def _cols_for(model):
    n = getattr(model, "n_features_", None) or getattr(model, "n_features_in_", None)
    if n is None or n == len(BASE_COLS):
        return BASE_COLS
    return BASE_COLS + EXTRA_FEATURE_COLS


def fetch_odds(d1, d2):
    """評価/訓練期間の確定2連複オッズと着順をまとめて引く。"""
    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": ["nirenfuku"]}
    with get_engine().connect() as conn:
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r "
            "ON r.id=o.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND o.is_final=1 AND o.odds>0 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no,rr.arrival_order FROM race_results rr "
            "JOIN races r ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order IS NOT NULL"
        ), prm).fetchall()
    nf = defaultdict(dict)
    for rid, cb, o in od:
        nf[int(rid)][str(cb)] = float(o)
    order = defaultdict(dict)
    for rid, bn, ao in res:
        order[int(rid)][int(ao)] = str(bn)
    return nf, order


def model_races(model, cols, temp, d1, d2):
    """モデルの艇スコアから、レースごとに {win, pairs} を作る。

    補正を「モデルの確率の分布」で学習するために要る。市場の確率で学んだ
    補正はモデルには移らなかった（2026-08-24 実測: 窓1 +2.27 / 窓2 +0.12）。
    """
    nf, order = fetch_odds(d1, d2)
    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    Xf = df[cols].apply(pd.to_numeric, errors="coerce")
    df = df.assign(pl_score=model.predict(Xf.fillna(Xf.median()).values))
    out = []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        o, fin = nf.get(rid, {}), order.get(rid, {})
        if len(o) != 15 or 1 not in fin or 2 not in fin:
            continue
        top2 = {fin[1], fin[2]}
        scores = {int(r.boat_no): float(r.pl_score) for r in grp.itertuples()}
        if len(scores) < 6:
            continue
        win = pl.scores_to_win_probs(scores, temperature=temp)
        exp_s = pl.to_exp_scores(scores, temperature=temp)
        tot = sum(1.0 / v for v in o.values())
        pairs = {}
        for cb, v in o.items():
            a, b = sorted(int(x) for x in cb.split("-"))
            pairs[(a, b)] = {
                "pl": pl.joint_prob_nirenfuku(exp_s, a, b),
                "mkt": (1.0 / v) / tot,
                "hit": set(cb.split("-")) == top2,
            }
        if len(pairs) == 15:
            out.append({"win": win, "pairs": pairs})
    return out


def gap(groups):
    """[(p, q_market, hit)] のレース束 → 市場との差(%)。正ならこちらが良い。"""
    d = mk = 0.0
    for g in groups:
        for p, q, y in g:
            lp = -(math.log(clip(p)) if y else math.log(1 - clip(p)))
            lq = -(math.log(clip(q)) if y else math.log(1 - clip(q)))
            d += lq - lp
            mk += lq
    return d / mk * 100


def boot(groups, T=1200):
    random.seed(0)
    v = sorted(gap([random.choice(groups) for _ in groups]) for _ in range(T))
    return v[int(.025 * T)], v[int(.975 * T)]


def main():
    import lightgbm as lgb

    mpath, t1, t2, e1, e2 = sys.argv[1:6]
    on_model = "--train-on-model" in sys.argv
    if Path(mpath).resolve() == Path(PROD).resolve():
        raise SystemExit("本番モデルは評価期間を訓練に含む（in-sample）ので渡さないこと")

    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    model = joblib.load(mpath)
    cols = _cols_for(model)

    if on_model:
        print("1) 補正を学習（**モデルの艇確率**→市場の2連複, %s〜%s）" % (t1, t2))
        tr = model_races(model, cols, temp, t1, t2)
    else:
        print("1) 補正を学習（市場の単勝→市場の2連複, %s〜%s）" % (t1, t2))
        tr = load_market(t1, t2)
    print("   %d レース" % len(tr))
    X, Y = [], []
    for r in tr:
        f, keys, plp = featurize(r)
        X.append(f)
        Y.append([math.log(clip(r["pairs"][k]["mkt"]) / clip(p))
                  for k, p in zip(keys, plp)])
    corr = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.05,
                             num_leaves=31, min_child_samples=50, verbose=-1)
    corr.fit(np.vstack(X), np.concatenate(Y))
    print("   学習完了 %d行" % len(np.concatenate(Y)))

    print("2) モデルで評価期間を予測（%s〜%s）" % (e1, e2))
    df = build_features(e1, e2, include_target=True).dropna(subset=["target_win"])
    Xf = df[cols].apply(pd.to_numeric, errors="coerce")
    df = df.assign(pl_score=model.predict(Xf.fillna(Xf.median()).values))

    from sqlalchemy import text, bindparam
    prm = {"d1": e1, "d2": e2, "bts": ["nirenfuku"]}
    with get_engine().connect() as conn:
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r "
            "ON r.id=o.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND o.is_final=1 AND o.odds>0 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no,rr.arrival_order FROM race_results rr "
            "JOIN races r ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order IS NOT NULL"
        ), prm).fetchall()
    nf = defaultdict(dict)
    for rid, cb, o in od:
        nf[int(rid)][str(cb)] = float(o)
    order = defaultdict(dict)
    for rid, bn, ao in res:
        order[int(rid)][int(ao)] = str(bn)

    g_pl, g_fix = [], []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        o, fin = nf.get(rid, {}), order.get(rid, {})
        if len(o) != 15 or 1 not in fin or 2 not in fin:
            continue
        top2 = {fin[1], fin[2]}
        scores = {int(r.boat_no): float(r.pl_score) for r in grp.itertuples()}
        if len(scores) < 6:
            continue
        win = pl.scores_to_win_probs(scores, temperature=temp)
        exp_s = pl.to_exp_scores(scores, temperature=temp)
        tot = sum(1.0 / v for v in o.values())
        race = {"win": win, "pairs": {}}
        for cb, v in o.items():
            a, b = sorted(int(x) for x in cb.split("-"))
            race["pairs"][(a, b)] = {
                "pl": pl.joint_prob_nirenfuku(exp_s, a, b),
                "mkt": (1.0 / v) / tot,
                "hit": set(cb.split("-")) == top2,
            }
        if len(race["pairs"]) != 15:
            continue
        f, keys, plp = featurize(race)
        adj = plp * np.exp(corr.predict(f))
        adj = adj / adj.sum()
        g_pl.append([(race["pairs"][k]["pl"], race["pairs"][k]["mkt"],
                      race["pairs"][k]["hit"]) for k in keys])
        g_fix.append([(a, race["pairs"][k]["mkt"], race["pairs"][k]["hit"])
                      for k, a in zip(keys, adj)])

    print("   %d レース / %d 組合せ" % (len(g_pl), len(g_pl) * 15))
    print()
    print("=== モデルの2連複確率 vs 市場 ===")
    a, (la, ha) = gap(g_pl), boot(g_pl)
    b, (lb, hb) = gap(g_fix), boot(g_fix)
    print("  現行（PLのまま）  %+.2f%%  [%+.2f%%, %+.2f%%]" % (a, la, ha))
    print("  提案（学習補正）  %+.2f%%  [%+.2f%%, %+.2f%%]" % (b, lb, hb))
    print()
    print("  改善 %+.2f ポイント" % (b - a))
    if b > 0 and lb > 0:
        print("  → 市場を上回った（区間が0を外している）")
    elif b > a:
        print("  → 改善したが、まだ市場に届いていない")
    else:
        print("  → 効果なし。市場で学んだ補正はモデルには移らない")


if __name__ == "__main__":
    main()
