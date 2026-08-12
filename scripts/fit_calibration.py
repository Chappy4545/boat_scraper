"""モデルの自信過剰を較正する関数を学習し、効果を検証する。

実測（2026-08-11）: モデルが40%と言うとき実際は30%しか当たらない（-10pt）。
EV = 確率 × オッズ なので、EV 1.2 と思っていたものが実は 0.9 だった。
較正が直れば的中率と回収率が同時に上がる。

方法:
  訓練期間の全組合せについて (モデル確率, 実際に当たったか) を集め、
  Isotonic回帰（単調性を保つ較正）で「モデル確率 → 実確率」の対応を学習する。
  単調性を保つので、モデルの順位付けは壊さずに確率だけを正す。

検証は必ず訓練期間より後の未見データで行う。

使い方: python scripts/fit_calibration.py <ranker> <train_to> <test_from> <test_to>
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features.builder import build_features, FEATURE_COLS
from src.models import plackett_luce as pl
from src.ingestion.database import get_engine
from src.utils.helpers import load_config

BT = "nirenfuku"
OUT = Path("data/processed/models/calibration_nirenfuku.json")


def collect(ranker, df, engine, d1, d2, need_odds: bool):
    """(モデル確率, 当たったか, オッズ, 確定払戻) を集める"""
    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": [BT]}
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
    won = defaultdict(set)
    for r in pay:
        won[int(r[0])].add(str(r[1]))
    odds = defaultdict(dict)
    for rid, cb, o in od:
        odds[int(rid)][str(cb)] = float(o)

    sub = df[(df["race_date"].astype(str) >= d1) & (df["race_date"].astype(str) <= d2)]
    rows = []
    for race_id, g in sub.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in won:
            continue
        if need_odds and rid not in odds:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        for c in pl.all_bet_probs(scores, temperature=1.0).get(BT, []):
            cb = c["combination"]
            rows.append({
                "p": float(c["model_prob"]),
                "y": 1 if cb in won[rid] else 0,
                "odds": odds[rid].get(cb),
                "payout": payout.get((rid, cb)),
            })
    return pd.DataFrame(rows)


def report(tag, sel):
    if len(sel) < 20:
        print(f"    {tag:<22}{len(sel):>6}  （少なすぎ）"); return
    stake = len(sel) * 100
    ret = sel["payout"].fillna(0).sum()
    print(f"    {tag:<22}{len(sel):>6,}{sel['y'].mean()*100:>7.1f}%"
          f"{sel['p_used'].mean()*100:>9.1f}%{ret/stake*100:>9.1f}%")


def main():
    ranker_path, train_to, test_from, test_to = sys.argv[1:5]
    engine = get_engine()
    ranker = joblib.load(ranker_path)

    df = build_features(None, test_to, include_target=True).dropna(subset=["target_win"])
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    df = df.assign(_score=ranker.predict(X.fillna(X.median()).values))

    print(f"較正を学習: 〜{train_to} / 検証: {test_from}〜{test_to}")
    tr = collect(ranker, df, engine, "2026-01-01", train_to, need_odds=False)
    print(f"  学習データ: {len(tr):,} 組合せ")

    # Isotonic回帰: 単調性を保ったまま確率を実測に合わせる
    from sklearn.isotonic import IsotonicRegression
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(tr["p"].values, tr["y"].values)

    # 較正前後の対応を保存（本番で使うため）
    grid = np.linspace(0.0, 1.0, 201)
    mapped = iso.predict(grid)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "bet_type": BT,
        "trained_to": train_to,
        "n_samples": int(len(tr)),
        "grid": [round(float(x), 4) for x in grid],
        "calibrated": [round(float(x), 5) for x in mapped],
    }, ensure_ascii=False), encoding="utf-8")
    print(f"  較正テーブルを保存: {OUT}")

    print("\n  【較正の中身】モデルがこう言うとき → 実際はこれくらい")
    for v in [0.10, 0.20, 0.30, 0.40, 0.50, 0.60]:
        print(f"    {v*100:.0f}% → {float(iso.predict([v])[0])*100:5.1f}%")

    te = collect(ranker, df, engine, test_from, test_to, need_odds=True)
    te = te[te["odds"].notna() & te["odds"].between(1.5, 50.0)]
    if te.empty:
        print("\n検証データなし"); return
    print(f"\n検証データ: {len(te):,} 組合せ")

    print("\n  【較正前】確率とEVをそのまま使う")
    print(f"    {'条件':<22}{'本数':>6}{'的中率':>8}{'予測平均':>10}{'回収率':>10}")
    te["p_used"] = te["p"]
    te["ev"] = te["p"] * te["odds"]
    for th in [0.20, 0.30, 0.40]:
        report(f"p>={th:.2f} & EV>=1.2", te[(te["p_used"] >= th) & (te["ev"] >= 1.2)])

    print("\n  【較正後】確率を較正してからEVを計算")
    te["p_cal"] = iso.predict(te["p"].values)
    te["p_used"] = te["p_cal"]
    te["ev_cal"] = te["p_cal"] * te["odds"]
    for th in [0.20, 0.30, 0.40]:
        report(f"p>={th:.2f} & EV>=1.2", te[(te["p_used"] >= th) & (te["ev_cal"] >= 1.2)])


if __name__ == "__main__":
    main()
