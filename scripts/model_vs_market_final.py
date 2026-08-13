"""モデルの確率は市場より正確か。賭式ごとに直接比べる。

これが利益の出る／出ないを決める。舟券は pari-mutuel（賭け金を集めて
分配する方式）なので、払戻は最終的な売上構成＝確定オッズで決まる。
つまり「安いうちに買う」は効かず、勝つ道は1つしかない:

    モデルの確率が、確定オッズが示す市場の確率より、控除率(25.8%)を
    超えて正確であること。

同じくらいの正確さなら、控除率のぶんだけ必ず負ける。実際どの賭式でも
回収率が 79〜100% に収まっているのはそのためではないか、を確かめる。

対数損失（小さいほど正確）で比べる。母数は同じ行なので公平な比較になる。
ただし bets に載っているのはモデルが評価した組合せだけなので、
「モデルが選びそうな領域での勝負」であることに注意。

使い方:
    python scripts/model_vs_market_final.py
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "boatrace.db"

NAME = {"tansho": "単勝", "nirentan": "2連単", "nirenfuku": "2連複",
        "sanrenfuku": "3連複", "sanrentan": "3連単"}

# 賭式ごとの組合せ数。市場確率の正規化が完全かを確かめるために使う。
N_COMB = {"tansho": 6, "nirentan": 30, "nirenfuku": 15,
          "sanrenfuku": 20, "sanrentan": 120}

LOAD = """
WITH full_races AS (
    SELECT race_id, bet_type
      FROM odds
     WHERE is_final = 1 AND odds > 0
     GROUP BY race_id, bet_type
    HAVING COUNT(*) = :n
),
mkt AS (
    SELECT o.race_id, o.bet_type, o.combination, o.odds,
           (1.0 / o.odds) / SUM(1.0 / o.odds)
               OVER (PARTITION BY o.race_id, o.bet_type) AS market_prob
      FROM odds o
      JOIN full_races f ON f.race_id = o.race_id AND f.bet_type = o.bet_type
     WHERE o.is_final = 1 AND o.odds > 0
)
SELECT b.model_prob, m.market_prob, b.is_hit
  FROM bets b
  JOIN races r ON r.id = b.race_id
  JOIN mkt   m ON m.race_id = b.race_id
              AND m.bet_type = b.bet_type
              AND m.combination = b.combination
 WHERE b.bet_type = :bt
   AND b.is_hit IS NOT NULL
   AND b.model_prob IS NOT NULL
   AND date(b.created_at) <= r.race_date
"""


def logloss(pairs: list[tuple[float, int]]) -> float:
    tot = 0.0
    for p, y in pairs:
        p = min(max(p, 1e-6), 1 - 1e-6)
        tot += -(math.log(p) if y else math.log(1 - p))
    return tot / len(pairs)


def main() -> None:
    conn = sqlite3.connect(DB)
    print("対数損失は小さいほど正確。控除率 25.8% を超えて勝っていなければ")
    print("どう選んでも回収率は 100% に届かない。\n")
    print(f"{'賭式':7}{'件数':>9}{'モデル':>10}{'市場':>10}{'差':>9}  判定")

    for bt, n in N_COMB.items():
        rows = conn.execute(LOAD, {"n": n, "bt": bt}).fetchall()
        if len(rows) < 300:
            print(f"{NAME[bt]:7}{len(rows):>9,}  （少なすぎて判断不能）")
            continue
        model = logloss([(r[0], r[2]) for r in rows])
        market = logloss([(r[1], r[2]) for r in rows])
        diff = market - model                     # 正ならモデルの勝ち
        rel = diff / market * 100
        verdict = f"モデルが {rel:+.1f}%" if diff > 0 else f"市場が {-rel:.1f}% 優位"
        print(f"{NAME[bt]:7}{len(rows):>9,}{model:>10.5f}{market:>10.5f}"
              f"{diff:>+9.5f}  {verdict}")

    print("\n必要な優位の目安: 控除率 25.8% を埋めるには、市場より")
    print("相当はっきり正確でなければならない。数%の差では届かない。")


if __name__ == "__main__":
    main()
