"""単勝を偏りなく評価する。

bets テーブル経由だと「オッズ1.5未満は除外」などの運用フィルタを通った
あとの買い目しか残らず（全期間で 336 件）、単勝が使えるかどうかを判断できない。
predictions テーブルには全レース×全艇の win_prob があるので、そこから
確定オッズと着順を突き合わせれば、絞り込み前の姿が見える。

単勝を見る理由: 組合せ数が少ないほどモデルの優位が残ることが分かっている。
    2連複(15通り) 100% / 3連複(20通り) 79% / 3連単(120通り) 79%
単勝は6通りで最も少ない。

使い方:
    python scripts/analyze_tansho.py
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "boatrace.db"

# 全艇ぶんの (モデル確率, 確定オッズ, 市場確率, 1着だったか)。
#   date(created_at) <= race_date … レース後に作られた予測を除く
#
# 6艇そろったレースに限る理由:
# 単勝オッズは 20,782 行中 3,334 行（16%）が 0 以下で入っている。これを
# 落とすと1レースが4〜5艇になり、1/odds を正規化した市場確率が過大になる
# （実測で平均 0.2165、本来 0.1667）。市場の対数損失が 0.61 という一様予測
# より悪い値になって初めて気づいた。欠けた集合で市場と比べてはいけない。
LOAD = """
WITH full_races AS (
    SELECT race_id
      FROM odds
     WHERE bet_type = 'tansho' AND is_final = 1 AND odds > 0
     GROUP BY race_id
    HAVING COUNT(*) = 6
),
mkt AS (
    SELECT race_id, combination, odds,
           (1.0 / odds) / SUM(1.0 / odds) OVER (PARTITION BY race_id) AS market_prob
      FROM odds
     WHERE bet_type = 'tansho' AND is_final = 1 AND odds > 0
       AND race_id IN (SELECT race_id FROM full_races)
)
SELECT p.win_prob,
       m.odds,
       m.market_prob,
       CASE WHEN rr.arrival_order = 1 THEN 1 ELSE 0 END AS won
  FROM predictions p
  JOIN races r  ON r.id = p.race_id
  JOIN mkt   m  ON m.race_id = p.race_id
               AND m.combination = CAST(p.boat_no AS TEXT)
  JOIN race_results rr ON rr.race_id = p.race_id AND rr.boat_no = p.boat_no
 WHERE p.win_prob IS NOT NULL
   AND date(p.created_at) <= r.race_date
"""


def summarise(sel: list[tuple]) -> str:
    """賭け金は一律100円。単勝の払戻は オッズ×100 円。"""
    n = len(sel)
    if n == 0:
        return f"{0:>7,}本"
    wins = sum(1 for o, w in sel if w)
    paid = sum(o for o, w in sel if w)          # 100円あたりの倍率の合計
    hr = wins / n
    roi = paid / n
    avg = (paid / wins) if wins else 0.0
    se = math.sqrt(hr * (1 - hr)) * avg / math.sqrt(n)
    if n < 60:
        return f"{n:>7,}本  （少なすぎて判断不能）"
    return (f"{n:>7,}本  的中{hr * 100:5.1f}%  回収{roi * 100:5.0f}% ±{se * 100:.0f}"
            f"  平均配当{avg:4.1f}倍")


def main() -> None:
    conn = sqlite3.connect(DB)
    rows = conn.execute(LOAD).fetchall()

    # 健全性の確認。ここが合っていない集計は読んではいけない。
    # 6艇そろっていれば市場確率の平均は 1/6、1着は1レースに1艇。
    avg_mp = sum(r[2] for r in rows) / len(rows)
    wins = sum(r[3] for r in rows)
    races = wins            # 1レースに1着は1艇なので、勝者数＝レース数
    print(f"=== 単勝  {len(rows):,}艇 / {races:,}レース ===")
    print(f"  健全性: 市場確率の平均 {avg_mp:.4f}（期待 0.1667） / "
          f"1艇あたり {len(rows) / races:.2f}艇（期待 6.00）")
    if abs(avg_mp - 1 / 6) > 0.005:
        print("  ⚠ 集合が欠けています。以下の数字は信用できません。")
    print()

    print("【確率で絞る】モデルが「勝つ」と見た艇だけ買う")
    for th in (0.30, 0.40, 0.50, 0.60, 0.70):
        sel = [(r[1], r[3]) for r in rows if r[0] >= th]
        print(f"  win_prob >= {th:.2f}          {summarise(sel)}")
    print()

    print("【期待値で絞る】確率 × 確定オッズ")
    for th in (1.0, 1.1, 1.2, 1.5):
        sel = [(r[1], r[3]) for r in rows if r[0] * r[1] >= th]
        print(f"  EV >= {th:.1f}                {summarise(sel)}")
    print()

    print("【両方で絞る】勝つと見ていて、かつ期待値も合う")
    for p in (0.40, 0.50, 0.60):
        for ev in (1.0, 1.1, 1.2):
            sel = [(r[1], r[3]) for r in rows if r[0] >= p and r[0] * r[1] >= ev]
            print(f"  win_prob>={p:.2f} & EV>={ev:.1f}  {summarise(sel)}")
    print()

    print("【参考】")
    print(f"  全艇を買った場合            {summarise([(r[1], r[3]) for r in rows])}")
    print("  ランダムに買った場合                 回収  74%（控除率25.8%）")

    # 市場と比べてモデルがどれだけ当たっているか（対数損失。小さいほど良い）
    def logloss(prob_idx: int) -> float:
        tot = 0.0
        for r in rows:
            p = min(max(r[prob_idx], 1e-6), 1 - 1e-6)
            tot += -(math.log(p) if r[3] else math.log(1 - p))
        return tot / len(rows)

    print(f"\n  対数損失  モデル {logloss(0):.5f} / 市場 {logloss(2):.5f}"
          f"  → {'モデルの勝ち' if logloss(0) < logloss(2) else '市場の勝ち'}")


if __name__ == "__main__":
    main()
