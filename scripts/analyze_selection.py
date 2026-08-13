"""買い目の選び方を変えたら回収率がどう動くかを、記録済みの買い目で測る。

現行ルール（2連複・model_prob>=0.30・EV>=1.2）は健全な 677 本で ROI 96%。
控除率 25.8%（ランダムなら 74%）は上回っているが 100% に届いていない。
100% を超えるために変えられるのは「いつのオッズで選ぶか」と「何で選ぶか」の
2つで、どちらも既存データで測れる。

検証1: 朝のオッズ vs 締切のオッズ
    朝の買い目は朝のオッズで EV を出している。実際の運用は締切 20 分前に
    確定させるので、確定オッズ（is_final=1）で選び直したものと比べる。
    確定オッズは締切直前に見えていた値とほぼ同じなので、
    「締切間際に選ぶ」の近似になる。レース結果は使っていない。

検証2: EV で選ぶ vs edge で選ぶ
    edge = model_prob / market_prob。market_prob は確定オッズの
    1/odds を賭式ごとに正規化したもの（控除率を除いた市場の見立て）。
    EV は「確率×配当」なので大穴ほど大きくなり、推定誤差が上振れした
    組合せが選ばれやすい（optimizer's curse）。edge は倍率に依存しない。

使い方:
    python scripts/analyze_selection.py [--bet-type nirenfuku]
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "boatrace.db"

NAME = {"tansho": "単勝", "nirentan": "2連単", "nirenfuku": "2連複",
        "sanrenfuku": "3連複", "sanrentan": "3連単"}

# 記録済みの買い目に、確定オッズと市場確率を付けたもの。
#   date(created_at) <= race_date … レース後に生成された買い目を除く
#   is_hit IS NOT NULL            … 結果が出ているもの
LOAD = """
WITH mkt AS (
    SELECT race_id, bet_type, combination, odds,
           (1.0 / odds) / SUM(1.0 / odds)
               OVER (PARTITION BY race_id, bet_type) AS market_prob
      FROM odds
     WHERE is_final = 1 AND odds > 0
)
SELECT b.bet_type,
       b.model_prob,
       b.odds          AS morning_odds,
       m.odds          AS final_odds,
       m.market_prob,
       b.is_hit,
       COALESCE(b.actual_payout, 0) AS payout
  FROM bets b
  JOIN races r ON r.id = b.race_id
  JOIN mkt   m ON m.race_id = b.race_id
              AND m.bet_type = b.bet_type
              AND m.combination = b.combination
 WHERE b.is_hit IS NOT NULL
   AND b.odds IS NOT NULL
   AND b.model_prob IS NOT NULL
   AND date(b.created_at) <= r.race_date
"""


def summarise(rows: list[tuple]) -> tuple[int, float, float, float]:
    """(本数, 的中率, 回収率, 回収率の1SE) を返す。賭け金は一律100円。"""
    n = len(rows)
    if n == 0:
        return 0, 0.0, 0.0, 0.0
    hits = sum(1 for r in rows if r[0])
    paid = sum(r[1] for r in rows)
    hr = hits / n
    roi = paid / (n * 100)
    avg = (paid / hits / 100) if hits else 0.0
    se = math.sqrt(hr * (1 - hr)) * avg / math.sqrt(n) if n else 0.0
    return n, hr, roi, se


def show(label: str, rows: list[tuple], floor: int = 60) -> None:
    n, hr, roi, se = summarise(rows)
    if n < floor:
        print(f"  {label:34} {n:>6,}本  （少なすぎて判断不能）")
        return
    print(f"  {label:34} {n:>6,}本  的中{hr * 100:5.1f}%  "
          f"回収{roi * 100:5.0f}% ±{se * 100:.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bet-type", default="nirenfuku")
    args = ap.parse_args()
    bt = args.bet_type

    conn = sqlite3.connect(DB)
    data = [r for r in conn.execute(LOAD) if r[0] == bt]
    print(f"=== {NAME.get(bt, bt)}  対象 {len(data):,}件 "
          f"（レース前に作られ、確定オッズが揃っているもの）===\n")

    # 検証1: どのオッズで EV を出すか
    print("【検証1】どの時点のオッズで選ぶか  (model_prob>=0.30, EV>=1.2)")
    for label, idx in (("朝のオッズで選ぶ（現行の測定値）", 2),
                       ("締切のオッズで選ぶ", 3)):
        sel = [(r[5], r[6]) for r in data
               if r[1] >= 0.30 and r[1] * r[idx] >= 1.2]
        show(label, sel)
    print()

    # 検証2: EV で選ぶか edge で選ぶか（どちらも締切のオッズを使う）
    print("【検証2】何で選ぶか  (model_prob>=0.30, 締切オッズ)")
    sel = [(r[5], r[6]) for r in data if r[1] >= 0.30 and r[1] * r[3] >= 1.2]
    show("EV >= 1.2", sel)
    for th in (1.2, 1.5, 2.0, 3.0):
        sel = [(r[5], r[6]) for r in data
               if r[1] >= 0.30 and r[4] and r[1] / r[4] >= th]
        show(f"edge >= {th}", sel)
    print()

    # 参考: 何もしない場合（その賭式の全記録）
    print("【参考】")
    show("この賭式の全記録（絞り込みなし）", [(r[5], r[6]) for r in data])
    print(f"  {'ランダムに買った場合':34}         回収  74%（控除率25.8%）")


if __name__ == "__main__":
    main()
