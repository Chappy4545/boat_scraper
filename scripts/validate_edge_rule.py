"""「モデルが市場より高く評価している」を軸にしたルールを検証する。

地図（edge_grid）を買える値のオッズで作り直したところ、両期間で
100%を超えたのは対角線より左側＝モデル評価が市場評価を上回るマスだけだった。
右端（市場も高く評価＝人気どころ）は一貫して70%台で負ける。

現行R5は「確率30%以上 かつ EV1.2以上」で選ぶため、
市場も同意している人気どころ（負けるマス）を含んでしまう。
そこで乖離（モデル確率 ÷ 市場確率）を条件に加えて比べる。

検証条件: is_live=1 のオッズのみ / 回収は確定払戻

使い方: python scripts/validate_edge_rule.py <ranker> <from> <to>
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

# p=モデル確率 / k=市場確率 / ev=p*odds / edge=p/k
RULES = [
    ("現行R5  p>=.30 & ev>=1.2",      lambda p, k, ev, e: p >= .30 and ev >= 1.2),
    ("  ＋ edge>=1.5",                lambda p, k, ev, e: p >= .30 and ev >= 1.2 and e >= 1.5),
    ("  ＋ edge>=2.0",                lambda p, k, ev, e: p >= .30 and ev >= 1.2 and e >= 2.0),
    ("edge>=2.0 のみ",                lambda p, k, ev, e: e >= 2.0),
    ("edge>=2.0 & p>=.15",            lambda p, k, ev, e: e >= 2.0 and p >= .15),
    ("edge>=1.5 & p>=.15 & ev>=1.2",  lambda p, k, ev, e: e >= 1.5 and p >= .15 and ev >= 1.2),
    ("edge>=2.0 & p>=.20 & ev>=1.2",  lambda p, k, ev, e: e >= 2.0 and p >= .20 and ev >= 1.2),
]


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
        if rid not in odds or len(odds[rid]) < 15:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        inv = {cb: 1.0 / o for cb, o in odds[rid].items() if o > 0}
        tot = sum(inv.values())
        if tot <= 0:
            continue
        for c in pl.all_bet_probs(scores, temperature=temp).get(BT, []):
            cb = c["combination"]
            o = odds[rid].get(cb)
            if o is None or not (MIN_ODDS <= o <= MAX_ODDS):
                continue
            p = float(c["model_prob"])
            k = inv[cb] / tot
            picks.append((p, k, p * o, p / k if k > 0 else 0,
                          payout.get((rid, cb), 0.0), (rid, cb) in payout))

    if not picks:
        print("該当なし"); return
    n_races = len({1 for _ in picks})
    print(f"=== {d1}〜{d2}  候補 {len(picks):,} 件 ===")
    print(f"  {'ルール':<30}{'本数':>7}{'的中率':>8}{'回収率':>9}")
    for label, cond in RULES:
        sel = [x for x in picks if cond(x[0], x[1], x[2], x[3])]
        if len(sel) < 20:
            print(f"  {label:<30}{len(sel):>7}  （少なすぎ）"); continue
        stake = len(sel) * 100
        ret = sum(x[4] for x in sel)
        hits = sum(1 for x in sel if x[5])
        roi = ret / stake * 100
        mark = "  ←黒字" if roi > 100 else ""
        print(f"  {label:<30}{len(sel):>7,}{hits/len(sel)*100:>7.1f}%{roi:>8.1f}%{mark}")


if __name__ == "__main__":
    main()
