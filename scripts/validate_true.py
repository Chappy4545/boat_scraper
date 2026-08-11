"""買い目ルールを「正しい回収額」で検証し直す。

これまでの検証スクリプトは回収額を odds × 賭け金 で計算していた。
だが記録オッズはレース前の速報値のことがあり、確定値と大きくずれる。
実測（2連複・的中組合せで比較）:
    5月 59.6%一致 / 平均乖離29.9%   6月 32.2%一致 / 平均乖離51.0%
    7月 95.7%一致 / 平均乖離 3.9%   8月 98.9%一致 / 平均乖離 3.5%
つまり5-6月の成績は歪んだ回収額で算出されていた。

正しい扱い:
  選ぶとき  = 記録オッズ（買う時点で実際に見える値）
  回収額    = payouts（確定した払戻。100円あたり）

使い方: python scripts/validate_true.py <ranker> <from> <to>
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

RULES = [
    ("R5  p>=0.30 & EV>=1.2", lambda p, o, ev: p >= 0.30 and ev >= 1.2),
    ("    p>=0.30 & EV>=1.5", lambda p, o, ev: p >= 0.30 and ev >= 1.5),
    ("    p>=0.20 & EV>=1.2", lambda p, o, ev: p >= 0.20 and ev >= 1.2),
    ("    p>=0.40 & EV>=1.2", lambda p, o, ev: p >= 0.40 and ev >= 1.2),
    ("    EV>=1.2 のみ",      lambda p, o, ev: ev >= 1.2),
    ("    p>=0.30 のみ",      lambda p, o, ev: p >= 0.30),
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
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_final=1 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()

    # 的中組合せ -> 確定払戻（100円あたり）
    payout = {}
    for rid, cb, p in pay:
        payout[(int(rid), str(cb))] = float(p)
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

    print(f"=== {d1}〜{d2}  候補 {len(picks):,} 件 ===")
    print(f"  {'ルール':<26}{'本数':>7}{'的中率':>8}{'回収率(正)':>12}{'回収率(誤)':>12}")
    for label, cond in RULES:
        sel = [x for x in picks if cond(x[0], x[1], x[2])]
        if not sel:
            print(f"  {label:<26}{'該当なし':>7}"); continue
        stake = len(sel) * 100
        # 正: 確定払戻 / 誤: 記録オッズ×100（従来の計算）
        ret_true = sum(x[3] for x in sel if x[3] is not None)
        ret_odds = sum(x[1] * 100 for x in sel if x[3] is not None)
        hits = sum(1 for x in sel if x[3] is not None)
        print(f"  {label:<26}{len(sel):>7,}{hits/len(sel)*100:>7.1f}%"
              f"{ret_true/stake*100:>11.1f}%{ret_odds/stake*100:>11.1f}%")
    print("\n  回収率(正)=確定払戻ベース / (誤)=記録オッズベース（従来の誤った計算）")


if __name__ == "__main__":
    main()
