"""モデルと市場（オッズ）のどちらが正確かを実測し、混ぜたらどうなるかを見る。

現在の30特徴量に市場情報は1つも入っていない。市場は控除率を除けば
「他人の予測の集合」であり、直前情報など我々が持たない情報も織り込んでいる。
モデル単独・市場単独・両者の混合を同じ土俵で比べ、伸びしろの在り処を測る。

指標は対数損失（小さいほど良い）と、実際に賭けたときの回収率。

使い方: python scripts/model_vs_market.py <ranker> <from> <to>
"""
from __future__ import annotations

import logging
import math
import sys
import warnings
from collections import defaultdict

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.features.builder import build_features, FEATURE_COLS
from src.models import plackett_luce as pl
from src.ingestion.database import get_engine
from src.utils.helpers import load_config

BT = "nirenfuku"
MIN_EV, MIN_ODDS, MAX_ODDS = 1.2, 1.5, 50.0


def logloss(p, y):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def main():
    ranker_path, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    engine = get_engine()

    ranker = joblib.load(ranker_path)
    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    df = df.assign(_score=ranker.predict(X.fillna(X.median()).values))

    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": [BT]}
    with engine.connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.combination FROM payouts p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r ON r.id=o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_final=1 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()

    hits = defaultdict(set)
    for rid, cb in pay:
        hits[int(rid)].add(str(cb))
    odds = defaultdict(dict)
    for rid, cb, o in od:
        odds[int(rid)][str(cb)] = float(o)

    # レースごとに (組合せ, モデル確率, 市場確率, オッズ, 的中) を集める
    rows = []
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in odds or rid not in hits or len(odds[rid]) < 15:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        probs = {c["combination"]: float(c["model_prob"])
                 for c in pl.all_bet_probs(scores, temperature=temp).get(BT, [])}
        # 市場の含意確率: 1/odds を正規化（控除率を取り除く）
        inv = {cb: 1.0 / o for cb, o in odds[rid].items() if o > 0}
        tot = sum(inv.values())
        if tot <= 0:
            continue
        won = hits[rid]
        for cb, o in odds[rid].items():
            if cb not in probs:
                continue
            rows.append({
                "rid": rid, "cb": cb, "pm": probs[cb], "pk": inv[cb] / tot,
                "odds": o, "y": 1 if cb in won else 0,
            })

    if not rows:
        print("該当データなし"); return
    R = pd.DataFrame(rows)
    n_races = R["rid"].nunique()
    print(f"=== {d1}〜{d2}  {n_races:,}レース / {len(R):,}組合せ ===")

    # ── 1. どちらが正確か（対数損失。小さいほど良い）
    print("\n【予測精度】対数損失（小さいほど正確）")
    for name, col in [("モデル単独", "pm"), ("市場単独", "pk")]:
        ll = np.mean([logloss(p, y) for p, y in zip(R[col], R["y"])])
        print(f"  {name:<10} {ll:.5f}")
    best = None
    for w in [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0]:
        blend = w * R["pm"] + (1 - w) * R["pk"]
        ll = np.mean([logloss(p, y) for p, y in zip(blend, R["y"])])
        if best is None or ll < best[1]:
            best = (w, ll)
    print(f"  最良の混合   モデル{best[0]*100:.0f}% + 市場{(1-best[0])*100:.0f}% → {best[1]:.5f}")

    # ── 2. 実際に賭けたらどうなるか
    print("\n【回収率】確率30%以上 かつ EV1.2以上 で賭けた場合")
    def sim(prob_col, label, min_p=0.30):
        sel = R[(R[prob_col] >= min_p) & (R[prob_col] * R["odds"] >= MIN_EV)
                & (R["odds"] >= MIN_ODDS) & (R["odds"] <= MAX_ODDS)]
        if not len(sel):
            print(f"  {label:<28} 該当なし"); return
        stake = len(sel) * 100
        ret = (sel["odds"] * 100 * sel["y"]).sum()
        print(f"  {label:<28} {len(sel):>5}本 的中{sel['y'].mean()*100:5.1f}% ROI {ret/stake*100:6.1f}%")

    sim("pm", "モデル確率で選ぶ（現行R5）")
    R["pb"] = best[0] * R["pm"] + (1 - best[0]) * R["pk"]
    sim("pb", f"混合確率で選ぶ（モデル{best[0]*100:.0f}%）")
    # 市場より高く評価している組合せだけ（優位が明確なもの）
    R["edge"] = R["pm"] / R["pk"]
    for th in [1.2, 1.5, 2.0]:
        sel = R[(R["pm"] >= 0.30) & (R["edge"] >= th) & (R["pm"] * R["odds"] >= MIN_EV)
                & (R["odds"] >= MIN_ODDS) & (R["odds"] <= MAX_ODDS)]
        if len(sel):
            stake = len(sel) * 100
            ret = (sel["odds"] * 100 * sel["y"]).sum()
            print(f"  {'モデル/市場 >= ' + str(th):<28} {len(sel):>5}本 "
                  f"的中{sel['y'].mean()*100:5.1f}% ROI {ret/stake*100:6.1f}%")


if __name__ == "__main__":
    main()
