"""買い目の選び方(ルール)を複数比較する。

仮説: EV で選ぶ = モデルの推定誤差が上振れした組合せを選ぶ(optimizer's curse)。
      逆に model_prob(確信度)で選ぶ方が機能する。
      特に max_bets_per_race で EV 上位だけ取る現行方式は最悪の選び方のはず。

使い方: python scratch_rules.py <ranker_path> <date_from> <date_to>
"""
from __future__ import annotations

import json
import logging
import sys
import time
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

# (名前, 条件関数(p, ev), レース内の上位N本に絞るか)
RULES = [
    ("R1 EV>=1.2 (現行に近い)",        lambda p, ev: ev >= 1.2, None),
    ("R2 EV>=1.2 かつ 上位5本(現行)",   lambda p, ev: ev >= 1.2, 5),
    ("R3 EV>=2.0",                     lambda p, ev: ev >= 2.0, None),
    ("R4 p>=0.20 かつ EV>=1.2",        lambda p, ev: p >= 0.20 and ev >= 1.2, None),
    ("R5 p>=0.30 かつ EV>=1.2",        lambda p, ev: p >= 0.30 and ev >= 1.2, None),
    ("R6 p>=0.30 (EV条件なし)",        lambda p, ev: p >= 0.30, None),
    ("R7 p>=0.30 かつ EV>=1.0",        lambda p, ev: p >= 0.30 and ev >= 1.0, None),
    ("R8 p>=0.25 かつ EV>=1.1",        lambda p, ev: p >= 0.25 and ev >= 1.1, None),
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ranker_path, date_from, date_to = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    engine = get_engine()

    ranker = joblib.load(ranker_path)
    df = build_features(date_from, date_to, include_target=True)
    df = df.dropna(subset=["target_win"])
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    X = X.fillna(X.median()).values
    df = df.assign(_score=ranker.predict(X))

    from sqlalchemy import text, bindparam
    prm = {"d1": date_from, "d2": date_to, "bts": [BT]}
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

    agg = {name: [0.0, 0.0, 0, 0] for name, _, _ in RULES}  # stake, ret, n, hit

    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in odds or rid not in hits:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        probs = pl.all_bet_probs(scores, temperature=temp)
        cand = []
        for item in probs.get(BT, []):
            cb = item["combination"]
            o = odds[rid].get(cb)
            if o is None or not (MIN_ODDS <= o <= MAX_ODDS):
                continue
            p = float(item["model_prob"])
            cand.append((p, o, p * o, cb))
        if not cand:
            continue
        won = hits[rid]
        for name, cond, topn in RULES:
            sel = [c for c in cand if cond(c[0], c[2])]
            if topn:
                sel = sorted(sel, key=lambda c: -c[2])[:topn]
            a = agg[name]
            for p, o, ev, cb in sel:
                a[0] += 100.0; a[2] += 1
                if cb in won:
                    a[1] += 100.0 * o; a[3] += 1

    out = {}
    log(f"=== {date_from} 〜 {date_to} ({BT}) ===")
    log(f"{'ルール':<28} {'本数':>6} {'的中率':>7} {'ROI':>8} {'損益(100円/本)':>14}")
    for name, _, _ in RULES:
        st, rt, n, h = agg[name]
        roi = rt / st if st else 0
        out[name] = {"bets": n, "hits": h, "hit_rate": h / n if n else None,
                     "roi": roi, "profit": rt - st}
        log(f"{name:<28} {n:>6} {(h/n*100 if n else 0):>6.2f}% {roi*100:>7.1f}% {int(rt-st):>14,}")
    with open(f"scratch_rules_{date_from}_{date_to}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
