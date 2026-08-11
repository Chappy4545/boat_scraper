"""採用ルール(R5)のリスクを実測する — 資金曲線・最大DD・連敗。

ROI が黒字でも、途中でどれだけ減るか(ドローダウン)と何連敗するかを
知らずに実運用してはいけない。賭け金の置き方を変えて比較する。

使い方: python scripts/risk_profile.py <ranker_path> <date_from> <date_to>
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
MIN_P, MIN_EV = 0.30, 1.2
MIN_ODDS, MAX_ODDS = 1.5, 50.0
BANKROLL0 = 100_000


def max_drawdown(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    return float(np.max((peak - equity) / np.where(peak == 0, 1, peak)))


def longest_loss_run(hits: list[int]) -> int:
    best = cur = 0
    for h in hits:
        cur = 0 if h else cur + 1
        best = max(best, cur)
    return best


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

    date_of = dict(zip(df["race_id"], df["race_date"].astype(str)))

    # 日付順に買い目を並べる（資金曲線は時系列でなければ意味がない）
    picks = []
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in odds or rid not in hits:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        probs = pl.all_bet_probs(scores, temperature=temp)
        for item in probs.get(BT, []):
            p, cb = float(item["model_prob"]), item["combination"]
            o = odds[rid].get(cb)
            if o is None or not (MIN_ODDS <= o <= MAX_ODDS):
                continue
            if p < MIN_P or p * o < MIN_EV:
                continue
            picks.append((date_of.get(rid, ""), rid, p, o, 1 if cb in hits[rid] else 0))
    picks.sort(key=lambda x: (x[0], x[1]))

    if not picks:
        print("該当なし"); return

    def simulate(name, sizing, day_cap=None):
        bank = BANKROLL0
        eq, hl = [], []
        day, spent = None, 0.0
        for d, rid, p, o, y in picks:
            if d != day:
                day, spent = d, 0.0
            amt = sizing(bank, p, o)
            if day_cap is not None:
                amt = min(amt, max(0.0, day_cap - spent))
            amt = max(0, int(amt // 100) * 100)
            if amt <= 0:
                continue
            spent += amt
            bank += (o * amt - amt) if y else -amt
            eq.append(bank); hl.append(y)
        if not eq:
            print(f"  {name}: 賭け成立なし"); return
        eq = np.array(eq)
        staked = sum(1 for _ in hl)
        print(f"  {name}")
        print(f"    本数={len(hl)} 的中率={np.mean(hl)*100:.1f}% "
              f"最終資金={int(eq[-1]):,}円 (元本{BANKROLL0:,})")
        print(f"    最大DD={max_drawdown(eq)*100:.1f}%  最長連敗={longest_loss_run(hl)}  "
              f"最低資金={int(eq.min()):,}円")

    q = load_config()["money_management"]
    print(f"=== {d1}〜{d2}  R5 リスクプロファイル (買い目 {len(picks)} 本) ===")
    simulate("A. 現状(quarter_kelly, 1点上限2000, 日次上限なし=バグ再現)",
             lambda b, p, o: min(b * max(0, ((o - 1) * p - (1 - p)) / (o - 1)) / 4, 2000, b))
    simulate("B. 同上 + 日次上限10,000を正しく適用",
             lambda b, p, o: min(b * max(0, ((o - 1) * p - (1 - p)) / (o - 1)) / 4, 2000, b),
             day_cap=10_000)
    simulate("C. 定額 1,000円/本", lambda b, p, o: min(1000, b))
    simulate("D. 定額 500円/本", lambda b, p, o: min(500, b))
    simulate("E. 資金の0.5%/本 (複利)", lambda b, p, o: min(b * 0.005, b))


if __name__ == "__main__":
    main()
