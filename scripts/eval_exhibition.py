"""展示タイムの効果を、実運用と同じ条件で検証する。

これまでの検証には2つの誤りがあった:
  1. 回収額を odds×賭け金 で計算していた → 確定払戻(payouts)で計算する
  2. レース後に遡って取得した確定オッズで買い目を選んでいた
     → is_live=1（レース当日に取得＝買う時点で見えた値）だけを使う

ここでは訓練期間を切り、展示タイムあり/なしの2モデルを同条件で比べる。

使い方: python scripts/eval_exhibition.py
"""
from __future__ import annotations

import logging
import sys
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.features.builder import build_features, FEATURE_COLS
from src.models import plackett_luce as pl
from src.ingestion.database import get_engine

BT = "nirenfuku"
TRAIN_TO = "2026-06-21"
TEST_FROM, TEST_TO = "2026-06-22", "2026-08-11"
MIN_ODDS, MAX_ODDS = 1.5, 50.0
EX_COLS = ["ex_time_z", "ex_rank", "tilt_v"]

RULES = [
    ("p>=0.30 & EV>=1.2", lambda p, ev: p >= 0.30 and ev >= 1.2),
    ("p>=0.30 & EV>=1.5", lambda p, ev: p >= 0.30 and ev >= 1.5),
    ("p>=0.20 & EV>=1.2", lambda p, ev: p >= 0.20 and ev >= 1.2),
    ("p>=0.40 & EV>=1.2", lambda p, ev: p >= 0.40 and ev >= 1.2),
]


def prep(df):
    df = df.copy()
    df["exhibition_time"] = pd.to_numeric(df["exhibition_time"], errors="coerce")
    g = df.groupby("race_id")["exhibition_time"]
    df["ex_time_z"] = (df["exhibition_time"] - g.transform("mean")) / g.transform("std").replace(0, np.nan)
    df["ex_rank"] = g.rank(method="min")
    df["tilt_v"] = pd.to_numeric(df["tilt"], errors="coerce")
    df["_rank"] = np.where(df["target_win"] == 1, 1,
                    np.where(df["target_top2"] == 1, 2,
                      np.where(df["target_top3"] == 1, 3, 4)))
    return df


def train(tr, cols):
    import lightgbm as lgb
    tr = tr.sort_values("race_id")
    X = tr[cols].apply(pd.to_numeric, errors="coerce")
    med = X.median()
    y = (6 - tr["_rank"]).clip(lower=0).astype(int)
    grp = tr.groupby("race_id", sort=False).size().values
    m = lgb.LGBMRanker(objective="lambdarank", n_estimators=300, learning_rate=0.05,
                       num_leaves=31, min_child_samples=30, random_state=42, verbose=-1)
    m.fit(X.fillna(med), y, group=grp)
    return m, med


def evaluate(model, med, te, cols, payout, odds, label):
    X = te[cols].apply(pd.to_numeric, errors="coerce").fillna(med)
    te = te.assign(_s=model.predict(X))
    picks = []
    for rid, g in te.groupby("race_id", sort=False):
        rid = int(rid)
        if rid not in odds:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_s"])}
        if len(scores) < 6:
            continue
        for c in pl.all_bet_probs(scores, temperature=1.0).get(BT, []):
            o = odds[rid].get(c["combination"])
            if o is None or not (MIN_ODDS <= o <= MAX_ODDS):
                continue
            p = float(c["model_prob"])
            picks.append((p, o, p * o, payout.get((rid, c["combination"]))))
    print(f"\n  【{label}】特徴量{len(cols)}個")
    print(f"    {'ルール':<22}{'本数':>7}{'的中率':>8}{'回収率':>9}")
    out = {}
    for name, cond in RULES:
        sel = [x for x in picks if cond(x[0], x[2])]
        if not sel:
            print(f"    {name:<22}{'該当なし':>7}"); continue
        stake = len(sel) * 100
        ret = sum(x[3] for x in sel if x[3] is not None)
        hits = sum(1 for x in sel if x[3] is not None)
        roi = ret / stake * 100
        out[name] = (len(sel), hits / len(sel) * 100, roi)
        mark = "  ←黒字" if roi > 100 else ""
        print(f"    {name:<22}{len(sel):>7,}{hits/len(sel)*100:>7.1f}%{roi:>8.1f}%{mark}")
    return out


def main():
    engine = get_engine()
    print(f"訓練 〜{TRAIN_TO} / 検証 {TEST_FROM}〜{TEST_TO}")
    print("検証は is_live=1（買う時点で見えた値）のオッズのみ、回収は確定払戻\n")

    df = build_features(None, TEST_TO, include_target=True).dropna(subset=["target_win"])
    df = prep(df)
    df = df[df["exhibition_time"].notna()]

    from sqlalchemy import text, bindparam
    prm = {"d1": TEST_FROM, "d2": TEST_TO, "bts": [BT]}
    with engine.connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.combination,p.payout FROM payouts p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r ON r.id=o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_live=1 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()

    payout = {(int(r[0]), str(r[1])): float(r[2]) for r in pay}
    odds = defaultdict(dict)
    for rid, cb, o in od:
        odds[int(rid)][str(cb)] = float(o)

    tr = df[df["race_date"].astype(str) <= TRAIN_TO]
    te = df[(df["race_date"].astype(str) >= TEST_FROM)
            & df["race_id"].isin(list(odds.keys()))]
    print(f"訓練 {tr['race_id'].nunique():,} レース / 検証 {te['race_id'].nunique():,} レース")

    base = [c for c in FEATURE_COLS if c in df.columns]
    m1, md1 = train(tr, base)
    r1 = evaluate(m1, md1, te, base, payout, odds, "展示なし（現行）")
    m2, md2 = train(tr, base + EX_COLS)
    r2 = evaluate(m2, md2, te, base + EX_COLS, payout, odds, "展示あり")

    print("\n  【差】")
    for name, _ in RULES:
        if name in r1 and name in r2:
            print(f"    {name:<22} 本数 {r2[name][0]-r1[name][0]:+5d}  "
                  f"回収率 {r2[name][2]-r1[name][2]:+6.1f}pt")


if __name__ == "__main__":
    main()
