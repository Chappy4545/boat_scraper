"""選別した買い目の中で、確信度の閾値を上げると的中率と回収率がどう動くか。

「選別した買い目で的中率を上げる」には2つの道がある:
  A. 閾値を上げる  → 的中率は上がるがオッズが下がる（回収率は下がりうる）
  B. モデルを良くする → 同じオッズ帯で的中率が上がる（純粋な改善）

どちらが効くのかを見極めるため、まず A の効き方を実測する。
検証は is_live=1（買う時点で見えた値）のオッズ、回収は確定払戻。

使い方: python scripts/threshold_sweep.py <ranker> <from> <to>
"""
from __future__ import annotations

import logging
import sys
import warnings
from collections import defaultdict

import joblib
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.features.builder import build_features, FEATURE_COLS
from src.models import plackett_luce as pl
from src.ingestion.database import get_engine
from src.utils.helpers import load_config

BT = "nirenfuku"
MIN_ODDS, MAX_ODDS = 1.5, 50.0


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

    picks = []
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in odds:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        for c in pl.all_bet_probs(scores, temperature=temp).get(BT, []):
            cb = c["combination"]
            o = odds[rid].get(cb)
            if o is None or not (MIN_ODDS <= o <= MAX_ODDS):
                continue
            p = float(c["model_prob"])
            picks.append((p, o, p * o, payout.get((rid, cb))))

    if not picks:
        print("該当なし"); return
    print(f"=== {d1}〜{d2}  候補 {len(picks):,} 件（2連複・買える値のオッズのみ）===")
    print("  EV>=1.2 を満たすものの中で、確信度の閾値を変えた場合")
    print(f"  {'閾値':<10}{'本数':>7}{'的中率':>8}{'予測平均':>9}{'平均オッズ':>10}{'回収率':>9}")
    for th in [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]:
        sel = [x for x in picks if x[0] >= th and x[2] >= 1.2]
        if len(sel) < 30:
            print(f"  {f'p>={th:.2f}':<10}{len(sel):>7}  （少なすぎ）"); continue
        stake = len(sel) * 100
        ret = sum(x[3] for x in sel if x[3] is not None)
        hits = sum(1 for x in sel if x[3] is not None)
        rate = hits / len(sel) * 100
        pred = sum(x[0] for x in sel) / len(sel) * 100
        avg_o = sum(x[1] for x in sel) / len(sel)
        roi = ret / stake * 100
        mark = "  ←黒字" if roi > 100 else ""
        print(f"  {f'p>={th:.2f}':<10}{len(sel):>7,}{rate:>7.1f}%{pred:>8.1f}%"
              f"{avg_o:>9.1f}倍{roi:>8.1f}%{mark}")
    print("\n  予測平均 > 的中率 なら、その帯でモデルは自信過剰。")
    print("  閾値を上げると的中率は上がるが平均オッズは下がる（回収率は別物）。")


if __name__ == "__main__":
    main()
