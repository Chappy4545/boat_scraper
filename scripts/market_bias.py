"""市場が「どこで」間違っているかを測る — 優位の源泉の特定。

モデルは市場より不正確（対数損失で負ける）のに、確率30%以上の帯では
回収率が100%を超える。矛盾ではなく、市場が特定の領域で系統的に
偏っているなら説明がつく（favorite-longshot bias）。

市場の含意確率（控除率を除いて正規化）と実際の的中率を帯別に比べ、
「市場が過小評価している領域」を特定する。そこが我々の取り分になる。

使い方: python scripts/market_bias.py <from> <to>
"""
from __future__ import annotations

import logging
import sys
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.ingestion.database import get_engine

BT = "nirenfuku"
BANDS = [(0, .02), (.02, .05), (.05, .10), (.10, .15), (.15, .20),
         (.20, .30), (.30, .40), (.40, .55), (.55, 1.01)]


def main():
    d1, d2 = sys.argv[1], sys.argv[2]
    engine = get_engine()
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

    rows = []
    for rid, m in odds.items():
        if len(m) < 15 or rid not in hits:
            continue
        inv = {cb: 1.0 / o for cb, o in m.items() if o > 0}
        tot = sum(inv.values())
        won = hits[rid]
        for cb, o in m.items():
            rows.append({"pk": inv[cb] / tot, "odds": o, "y": 1 if cb in won else 0})
    if not rows:
        print("該当なし"); return
    R = pd.DataFrame(rows)

    print(f"=== {d1}〜{d2}  {len(R):,}組合せ（{len(odds):,}レース）===")
    print("市場の含意確率と実際の的中率の比較")
    print(f"  {'市場が言う確率':<14}{'件数':>7}{'市場':>8}{'実際':>8}{'実際/市場':>10}{'回収率':>9}")
    for lo, hi in BANDS:
        s = R[(R["pk"] >= lo) & (R["pk"] < hi)]
        if len(s) < 30:
            continue
        pk, act = s["pk"].mean(), s["y"].mean()
        roi = (s["odds"] * s["y"]).sum() / len(s)
        flag = "  ← 市場が過小評価" if act / pk > 1.03 else ""
        print(f"  {f'{lo*100:.0f}〜{hi*100:.0f}%':<14}{len(s):>7,}{pk*100:>7.1f}%{act*100:>7.1f}%"
              f"{act/pk:>9.2f}{roi*100:>8.1f}%{flag}")
    print("\n  ※ 回収率は「その帯を全部買った場合」。控除率25.8%なので")
    print("     何もしなければ74%前後になるのが基準。")


if __name__ == "__main__":
    main()
