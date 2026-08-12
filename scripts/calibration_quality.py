"""較正の良し悪しを、オッズ抜きで大量データを使って測る。

較正の学習・検証に必要なのは「モデル確率」と「実際に当たったか」だけで、
オッズは要らない（オッズが要るのは回収率の検証だけ）。
前回はオッズがある4,428レースに絞ってしまい、87本で判断しようとしていた。
確定払戻は33,896レース分あるので、そちらで測る。

指標:
  - 帯別の 予測 vs 実際（自信過剰がどこで起きているか）
  - Brier スコア（小さいほど確率の精度が高い）

使い方: python scripts/calibration_quality.py <ranker> <cal_from> <cal_to> <test_from> <test_to>
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
BANDS = [(0, .05), (.05, .10), (.10, .15), (.15, .20), (.20, .30),
         (.30, .40), (.40, .50), (.50, 1.01)]


def collect(df, engine, d1, d2):
    """オッズ不要。確定払戻から「当たったか」だけを取る。"""
    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": [BT]}
    with engine.connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.combination FROM payouts p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
    won = defaultdict(set)
    for rid, cb in pay:
        won[int(rid)].add(str(cb))

    sub = df[(df["race_date"].astype(str) >= d1) & (df["race_date"].astype(str) <= d2)]
    P, Y = [], []
    for race_id, g in sub.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in won:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        for c in pl.all_bet_probs(scores, temperature=1.0).get(BT, []):
            P.append(float(c["model_prob"]))
            Y.append(1 if c["combination"] in won[rid] else 0)
    return np.array(P), np.array(Y)


def table(p, y, label):
    print(f"\n  【{label}】Brier={np.mean((p - y) ** 2):.5f}（小さいほど良い）")
    print(f"    {'帯':<12}{'件数':>9}{'予測':>8}{'実際':>8}{'差':>8}")
    for lo, hi in BANDS:
        m = (p >= lo) & (p < hi)
        if m.sum() < 100:
            continue
        pr, ac = p[m].mean(), y[m].mean()
        print(f"    {f'{lo*100:.0f}〜{hi*100:.0f}%':<12}{m.sum():>9,}"
              f"{pr*100:>7.1f}%{ac*100:>7.1f}%{(ac-pr)*100:>+7.1f}pt")


def main():
    ranker_path, cal_from, cal_to, test_from, test_to = sys.argv[1:6]
    engine = get_engine()
    ranker = joblib.load(ranker_path)

    df = build_features(None, test_to, include_target=True).dropna(subset=["target_win"])
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    df = df.assign(_score=ranker.predict(X.fillna(X.median()).values))

    print(f"較正の学習: {cal_from}〜{cal_to} / 検証: {test_from}〜{test_to}")
    pc, yc = collect(df, engine, cal_from, cal_to)
    pt, yt = collect(df, engine, test_from, test_to)
    print(f"  学習 {len(pc):,} 組合せ / 検証 {len(pt):,} 組合せ")

    from sklearn.isotonic import IsotonicRegression
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(pc, yc)

    table(pt, yt, "較正なし")
    table(iso.predict(pt), yt, "較正あり")

    out = Path("data/processed/models/calibration_nirenfuku.json")
    grid = np.linspace(0.0, 1.0, 201)
    out.write_text(json.dumps({
        "bet_type": BT, "calibrated_on": f"{cal_from}..{cal_to}",
        "n_samples": int(len(pc)),
        "grid": [round(float(x), 4) for x in grid],
        "calibrated": [round(float(x), 5) for x in iso.predict(grid)],
    }, ensure_ascii=False), encoding="utf-8")
    print(f"\n較正テーブル保存: {out}")


if __name__ == "__main__":
    main()
