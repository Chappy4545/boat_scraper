"""的中率と回収率のトレードオフを実測する。

「的中率を上げたい」は自然な願いだが、買い目を増やせば的中率は上がる一方で
1本あたりの配当は下がる。控除率25.8%の市場では、広く買うほど確実に負ける。
どこまで的中率を上げられて、その代償がいくらかを数字で示す。

各レースでモデル確率の高い順に N 点買った場合の
  的中率（= N点のどれかが当たる確率）と 回収率 を N ごとに出す。

使い方: python scripts/hitrate_tradeoff.py <ranker> <from> <to>
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

    # レースごとに、モデル確率の高い順に並べた組合せ
    races = []
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in odds or len(odds[rid]) < 15:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        combos = sorted(pl.all_bet_probs(scores, temperature=temp).get(BT, []),
                        key=lambda c: -float(c["model_prob"]))
        races.append((rid, [c["combination"] for c in combos]))

    if not races:
        print("該当なし"); return

    print(f"=== {d1}〜{d2}  {len(races):,}レース（2連複・全15点中）===")
    print("  モデル確率の高い順に N 点買った場合")
    print(f"  {'買う点数':<10}{'的中率':>9}{'回収率':>9}{'1レース費用':>12}")
    for n in [1, 2, 3, 4, 5, 6, 8, 10, 12, 15]:
        hit = 0
        stake = ret = 0.0
        for rid, order in races:
            picks = order[:n]
            stake += 100 * n
            got = [payout.get((rid, cb)) for cb in picks]
            got = [x for x in got if x is not None]
            if got:
                hit += 1
                ret += sum(got)
        rate = hit / len(races) * 100
        roi = ret / stake * 100
        mark = "  ←黒字" if roi > 100 else ""
        print(f"  {f'{n}点':<10}{rate:>8.1f}%{roi:>8.1f}%{n*100:>11,}円{mark}")

    print("\n  ※ 何も考えず買えば回収率74%（控除率25.8%）。")
    print("     的中率を上げるには点数を増やすしかないが、費用も比例して増える。")


if __name__ == "__main__":
    main()
