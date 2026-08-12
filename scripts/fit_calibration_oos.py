"""較正を「未見データ」で学習して検証する。

前回わかったこと:
  訓練データ上ではモデルは控えめ（30%と言って34%当たる）なのに、
  未見データでは自信過剰（39.7%と言って29.6%）。
  つまり問題は較正ではなく過学習であり、訓練データから作った較正表では直らない。

そこで較正表を訓練データではなく「モデルにとって未見の実測」から作る。
ただし同じデータで学習と検証をすると意味がないので、
未見期間を前半（較正の学習）と後半（検証）に分ける。

使い方:
  python scripts/fit_calibration_oos.py <ranker> <cal_from> <cal_to> <test_from> <test_to>
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

BT = "nirenfuku"
OUT = Path("data/processed/models/calibration_nirenfuku.json")


def collect(df, engine, d1, d2, need_odds: bool):
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
        if rid not in won or (need_odds and rid not in odds):
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        for c in pl.all_bet_probs(scores, temperature=1.0).get(BT, []):
            cb = c["combination"]
            rows.append({"p": float(c["model_prob"]), "y": 1 if cb in won[rid] else 0,
                         "odds": odds[rid].get(cb), "payout": payout.get((rid, cb))})
    return pd.DataFrame(rows)


def show(te, col_p, col_ev, label):
    print(f"\n  【{label}】")
    print(f"    {'条件':<22}{'本数':>7}{'的中率':>8}{'予測平均':>10}{'回収率':>10}")
    for th in [0.20, 0.25, 0.30, 0.40]:
        sel = te[(te[col_p] >= th) & (te[col_ev] >= 1.2)]
        if len(sel) < 20:
            print(f"    {f'p>={th:.2f} & EV>=1.2':<22}{len(sel):>7}  （少なすぎ）"); continue
        stake = len(sel) * 100
        ret = sel["payout"].fillna(0).sum()
        roi = ret / stake * 100
        mark = "  ←黒字" if roi > 100 else ""
        print(f"    {f'p>={th:.2f} & EV>=1.2':<22}{len(sel):>7,}{sel['y'].mean()*100:>7.1f}%"
              f"{sel[col_p].mean()*100:>9.1f}%{roi:>9.1f}%{mark}")


def main():
    ranker_path, cal_from, cal_to, test_from, test_to = sys.argv[1:6]
    engine = get_engine()
    ranker = joblib.load(ranker_path)

    df = build_features(None, test_to, include_target=True).dropna(subset=["target_win"])
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    df = df.assign(_score=ranker.predict(X.fillna(X.median()).values))

    print(f"較正の学習: {cal_from}〜{cal_to}（モデルにとって未見）")
    print(f"検証      : {test_from}〜{test_to}\n")

    cal = collect(df, engine, cal_from, cal_to, need_odds=False)
    print(f"較正データ: {len(cal):,} 組合せ")

    from sklearn.isotonic import IsotonicRegression
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(cal["p"].values, cal["y"].values)

    print("  モデルがこう言うとき → 実際はこれくらい")
    for v in [0.10, 0.20, 0.30, 0.40, 0.50]:
        print(f"    {v*100:.0f}% → {float(iso.predict([v])[0])*100:5.1f}%")

    te = collect(df, engine, test_from, test_to, need_odds=True)
    te = te[te["odds"].notna() & te["odds"].between(1.5, 50.0)]
    if te.empty:
        print("\n検証データなし"); return
    print(f"\n検証データ: {len(te):,} 組合せ")

    te["ev"] = te["p"] * te["odds"]
    te["p_cal"] = iso.predict(te["p"].values)
    te["ev_cal"] = te["p_cal"] * te["odds"]
    show(te, "p", "ev", "較正なし")
    show(te, "p_cal", "ev_cal", "較正あり（未見データで学習）")

    grid = np.linspace(0.0, 1.0, 201)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "bet_type": BT, "calibrated_on": f"{cal_from}..{cal_to}",
        "n_samples": int(len(cal)),
        "grid": [round(float(x), 4) for x in grid],
        "calibrated": [round(float(x), 5) for x in iso.predict(grid)],
    }, ensure_ascii=False), encoding="utf-8")
    print(f"\n較正テーブル保存: {OUT}")


if __name__ == "__main__":
    main()
