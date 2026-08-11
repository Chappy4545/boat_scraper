"""全賭式を同じ基準で比べる — どの買い式が最も勝ちやすいか。

これまで 2連複しか本気で検証していなかった（単勝は一度も測っていない）。
賭式ごとに控除率も市場の歪み方も違うため、同じ土俵で並べる。

各賭式について:
  - 市場の控除率（sum(1/odds) から逆算）
  - モデル確率の帯別 回収率（未見データ）
  - 最良の条件と、その本数

使い方: python scripts/compare_bet_types.py <ranker> <from> <to>
"""
from __future__ import annotations

import logging
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

BET_TYPES = ["tansho", "nirenfuku", "nirentan", "sanrenfuku", "sanrentan"]
LABEL = {"tansho": "単勝", "nirenfuku": "2連複", "nirentan": "2連単",
         "sanrenfuku": "3連複", "sanrentan": "3連単"}
N_COMBOS = {"tansho": 6, "nirenfuku": 15, "nirentan": 30,
            "sanrenfuku": 20, "sanrentan": 120}
BANDS = [(.02, .05), (.05, .10), (.10, .15), (.15, .20), (.20, .30),
         (.30, .40), (.40, .55), (.55, 1.01)]
MIN_N = 40


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
    prm = {"d1": d1, "d2": d2, "bts": BET_TYPES}
    with engine.connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.bet_type,p.combination FROM payouts p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        od = conn.execute(text(
            "SELECT o.race_id,o.bet_type,o.combination,o.odds FROM odds o JOIN races r ON r.id=o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_final=1 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()

    hits = defaultdict(set)
    for rid, bt, cb in pay:
        hits[(int(rid), bt)].add(str(cb))
    odds = defaultdict(dict)
    for rid, bt, cb, o in od:
        odds[(int(rid), bt)][str(cb)] = float(o)

    rows = defaultdict(list)
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        probs = pl.all_bet_probs(scores, temperature=temp)
        for bt in BET_TYPES:
            key = (rid, bt)
            if key not in odds or key not in hits:
                continue
            if len(odds[key]) < N_COMBOS[bt]:
                continue          # オッズが欠けているレースは除外
            won = hits[key]
            inv_tot = sum(1.0 / o for o in odds[key].values() if o > 0)
            for c in probs.get(bt, []):
                o = odds[key].get(c["combination"])
                if o is None or o <= 0:
                    continue
                rows[bt].append({
                    "pm": float(c["model_prob"]), "odds": o,
                    "y": 1 if c["combination"] in won else 0,
                    "takeout": 1 - 1 / inv_tot,
                })

    print(f"=== {d1}〜{d2} ===")
    for bt in BET_TYPES:
        if not rows[bt]:
            print(f"\n【{LABEL[bt]}】データなし"); continue
        R = pd.DataFrame(rows[bt])
        take = R["takeout"].median() * 100
        n_races = len(R) // N_COMBOS[bt]
        print(f"\n【{LABEL[bt]}】{n_races:,}レース / 控除率 {take:.1f}% "
              f"（何も考えず買えば回収率 {100-take:.0f}%）")
        print(f"  {'モデル確率':<10}{'本数':>7}{'的中率':>8}{'回収率':>9}")
        best = None
        for lo, hi in BANDS:
            s = R[(R["pm"] >= lo) & (R["pm"] < hi)]
            if len(s) < MIN_N:
                continue
            roi = (s["odds"] * s["y"]).sum() / len(s) * 100
            mark = "  ←黒字" if roi > 100 else ""
            print(f"  {f'{lo*100:.0f}〜{hi*100:.0f}%':<10}{len(s):>7,}"
                  f"{s['y'].mean()*100:>7.1f}%{roi:>8.1f}%{mark}")
            if roi > 100 and (best is None or roi > best[1]):
                best = ((lo, hi), roi, len(s))
        if best:
            (lo, hi), roi, n = best
            print(f"  → 最良: 確率{lo*100:.0f}〜{hi*100:.0f}% で {roi:.0f}%（{n:,}本）")
        else:
            print("  → 黒字の帯なし")


if __name__ == "__main__":
    main()
