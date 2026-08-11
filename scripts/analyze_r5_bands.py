"""R5で選別した「後」の買い目を、EV帯 / 確率帯 に分けて成績を見る。

UI が何を主役に据えるべきかを決めるための測定。
現在の PWA は EV 降順・EV帯見出し・EVを最大表示にしているが、
EV は optimizer's curse の温床でもある。R5 で絞った後でも
「EVが高いほど悪い」なら、UI は確率を主役にすべき。

使い方: python scripts/analyze_r5_bands.py <ranker> <from> <to>
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
MIN_P, MIN_EV = 0.30, 1.2
MIN_ODDS, MAX_ODDS = 1.5, 50.0

EV_BANDS = [(1.2, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 99)]
P_BANDS = [(0.30, 0.35), (0.35, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 1.01)]
O_BANDS = [(1.5, 3), (3, 5), (5, 8), (8, 15), (15, 50)]


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

    picks = []
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in odds or rid not in hits:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        for item in pl.all_bet_probs(scores, temperature=temp).get(BT, []):
            p, cb = float(item["model_prob"]), item["combination"]
            o = odds[rid].get(cb)
            if o is None or not (MIN_ODDS <= o <= MAX_ODDS):
                continue
            if p < MIN_P or p * o < MIN_EV:
                continue
            picks.append((p, o, p * o, 1 if cb in hits[rid] else 0))
    return picks


def report(title, picks, key, bands, fmt):
    print(f"  {title}")
    print(f"    {'帯':<12}{'本数':>6}{'的中率':>8}{'ROI':>8}")
    for lo, hi in bands:
        sel = [x for x in picks if lo <= key(x) < hi]
        if not sel:
            continue
        stake = len(sel) * 100
        ret = sum(100 * x[1] for x in sel if x[3])
        hr = sum(x[3] for x in sel) / len(sel)
        mark = "  ←黒字" if ret / stake > 1 else ""
        print(f"    {fmt(lo, hi):<12}{len(sel):>6}{hr*100:>7.1f}%{ret/stake*100:>7.1f}%{mark}")


def main():
    ranker, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    picks = collect(ranker, d1, d2)
    if not picks:
        print("該当なし"); return
    stake = len(picks) * 100
    ret = sum(100 * p[1] for p in picks if p[3])
    print(f"=== {d1}〜{d2}  R5選別後 {len(picks)}本  "
          f"的中率={sum(p[3] for p in picks)/len(picks)*100:.1f}%  ROI={ret/stake*100:.1f}% ===")
    report("EV帯別（UIが今 主役にしている軸）", picks, lambda x: x[2], EV_BANDS,
           lambda a, b: f"{a}〜{b}")
    report("確率帯別", picks, lambda x: x[0], P_BANDS,
           lambda a, b: f"{a*100:.0f}〜{b*100:.0f}%")
    report("オッズ帯別", picks, lambda x: x[1], O_BANDS,
           lambda a, b: f"{a}〜{b}倍")


if __name__ == "__main__":
    main()
