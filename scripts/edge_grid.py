"""モデル確率 × 市場確率 の2軸で回収率を見る — どこを買えば勝てるかの地図。

市場は 10〜30% 帯で系統的に過小評価している（実測: 実際/市場 = 1.08〜1.10）が、
帯を丸ごと買っても回収率は最良で 82.4%。控除率25.8%が重く、
「帯の中でどれが来るか」を market より正確に選べないと勝てない。

そこで2軸で切る:
  縦 = モデルが言う確率 / 横 = 市場が言う確率
両者が食い違う場所（モデルは高いと言うが市場は安く見ている）に
利益があるはずで、その位置と大きさを地図にする。

使い方: python scripts/edge_grid.py <ranker> <from> <to>
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

BT = "nirenfuku"
MIN_ODDS, MAX_ODDS = 1.5, 50.0
MODEL_BANDS = [(.05, .10), (.10, .15), (.15, .20), (.20, .30), (.30, .40), (.40, 1.01)]
MKT_BANDS = [(0, .05), (.05, .10), (.10, .15), (.15, .25), (.25, 1.01)]
MIN_N = 40   # これ未満のマスは判断材料にしない


def collect(ranker_path, d1, d2):
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
            # is_live=1 のみ: レース当日に取得した＝買う時点で実際に見えた値
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r ON r.id=o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_live=1 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()

    # 回収は確定払戻（100円あたり）。odds×100 で計算すると速報値の誤差が乗る
    payout = {(int(r[0]), str(r[1])): float(r[2]) for r in pay}
    hits = defaultdict(set)
    for r in pay:
        hits[int(r[0])].add(str(r[1]))
    odds = defaultdict(dict)
    for rid, cb, o in od:
        odds[int(rid)][str(cb)] = float(o)

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
        inv = {cb: 1.0 / o for cb, o in odds[rid].items() if o > 0}
        tot = sum(inv.values())
        won = hits[rid]
        for cb, o in odds[rid].items():
            if cb not in probs or not (MIN_ODDS <= o <= MAX_ODDS):
                continue
            rows.append({"pm": probs[cb], "pk": inv[cb] / tot, "odds": o,
                         "y": 1 if cb in won else 0,
                         "ret": payout.get((rid, cb), 0.0)})
    return pd.DataFrame(rows)


def show(R, title):
    print(f"\n=== {title}  {len(R):,}組合せ ===")
    print("  縦=モデルが言う確率 / 横=市場が言う確率 / 各マス: 本数と回収率")
    head = "  " + "モデル\\市場".ljust(12) + "".join(
        f"{f'{a*100:.0f}-{b*100:.0f}%':>13}" for a, b in MKT_BANDS)
    print(head)
    for mlo, mhi in MODEL_BANDS:
        line = "  " + f"{mlo*100:.0f}-{mhi*100:.0f}%".ljust(12)
        for klo, khi in MKT_BANDS:
            s = R[(R["pm"] >= mlo) & (R["pm"] < mhi) & (R["pk"] >= klo) & (R["pk"] < khi)]
            if len(s) < MIN_N:
                line += f"{'-':>13}"
                continue
            roi = s["ret"].sum() / (len(s) * 100) * 100
            line += f"{f'{len(s)}本 {roi:.0f}%':>13}"
        print(line)


def main():
    ranker, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    R = collect(ranker, d1, d2)
    if R.empty:
        print("該当なし"); return
    show(R, f"{d1}〜{d2}")
    print("\n  ※ 何も考えず買えば74%。100%超のマスだけが利益になる。")
    print("     '-' は40本未満で判断材料にならないマス。")


if __name__ == "__main__":
    main()
