"""賭式ごとに「何点買えば的中率いくつで、回収率いくつか」を実測する。

必要な的中率は賭式ごとに全く違う。単勝でオッズ1.8倍なら
的中率 1/1.8 = 55.6% を超えないと必ず負ける。
一方 2連複でオッズ10倍なら 10% で足りる。

選ぶのはモデル確率の高い順（オッズ不要）、回収は確定払戻。
この方法なら全期間（約33,000レース）を使えるため、
少ないサンプルに振り回されずに構造を見られる。

使い方: python scripts/hitrate_by_type.py <ranker> <from> <to>
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

TYPES = [("tansho", "単勝", 6, [1, 2, 3]),
         ("nirenfuku", "2連複", 15, [1, 2, 3, 5, 7]),
         ("sanrenfuku", "3連複", 20, [1, 2, 3, 5, 8]),
         ("sanrentan", "3連単", 120, [1, 3, 5, 10, 20])]


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
    prm = {"d1": d1, "d2": d2, "bts": [t[0] for t in TYPES]}
    with engine.connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.bet_type,p.combination,p.payout FROM payouts p "
            "JOIN races r ON r.id=p.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()

    payout = {(int(r[0]), str(r[1]), str(r[2])): float(r[3]) for r in pay}
    has = defaultdict(set)
    for r in pay:
        has[str(r[1])].add(int(r[0]))

    ranked = defaultdict(dict)   # bet_type -> race_id -> [combination...]
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        probs = pl.all_bet_probs(scores, temperature=temp)
        for bt, _, _, _ in TYPES:
            if rid not in has[bt]:
                continue
            cs = sorted(probs.get(bt, []), key=lambda c: -float(c["model_prob"]))
            ranked[bt][rid] = [c["combination"] for c in cs]

    print(f"=== {d1}〜{d2} ===")
    for bt, label, total, ns in TYPES:
        races = ranked[bt]
        if not races:
            print(f"\n【{label}】データなし"); continue
        print(f"\n【{label}】{len(races):,}レース（全{total}点中）")
        print(f"  {'点数':<8}{'的中率':>9}{'回収率':>9}{'損益分岐に必要な的中率':>24}")
        for n in ns:
            hit = 0
            ret = 0.0
            for rid, order in races.items():
                got = [payout.get((rid, bt, cb)) for cb in order[:n]]
                got = [x for x in got if x is not None]
                if got:
                    hit += 1
                    ret += sum(got)
            stake = len(races) * 100 * n
            rate = hit / len(races) * 100
            roi = ret / stake * 100
            # 的中時の平均配当から、損益分岐に必要な的中率を逆算
            avg_pay = (ret / hit / 100) if hit else 0
            need = (n / avg_pay * 100) if avg_pay else 0
            mark = "  ←黒字" if roi > 100 else ""
            print(f"  {f'{n}点':<8}{rate:>8.1f}%{roi:>8.1f}%{need:>22.1f}%{mark}")


if __name__ == "__main__":
    main()
