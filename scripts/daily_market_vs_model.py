"""その日、モデルと市場のどちらが当てていたかを日別に出す。

「今日は外れが多かった」だけでは、モデルが悪かったのか、その日が
誰にとっても難しかったのかが分からない。市場（確定オッズ）を同じ土俵に
置いて比べれば区別がつく:

    両方とも対数損失が大きい → 荒れた日。モデルのせいではない
    モデルだけ大きい         → その日モデルが外した
    モデルの方が小さい       → その日はモデルが市場に勝っていた

記録済みの model_prob（朝の予測）と、確定オッズから作る市場確率を、
同じ組合せの上で比べる。市場確率は 15 通り揃ったレースだけで作る
（欠けた集合で正規化すると意味のない値になる）。

使い方:
    python scripts/daily_market_vs_model.py [--from 2026-08-11]
"""
from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "boatrace.db"

# その日の 2連複 全組合せについて、モデル確率・市場確率・的中を並べる。
SQL = """
WITH full_races AS (
    SELECT o.race_id
      FROM odds o JOIN races r ON r.id = o.race_id
     WHERE r.race_date = :d AND o.bet_type = 'nirenfuku'
       AND o.is_final = 1 AND o.odds > 0
     GROUP BY o.race_id HAVING COUNT(*) = 15
),
mkt AS (
    SELECT o.race_id, o.combination, o.odds,
           (1.0 / o.odds) / SUM(1.0 / o.odds)
               OVER (PARTITION BY o.race_id) AS pk
      FROM odds o
      JOIN full_races f ON f.race_id = o.race_id
     WHERE o.bet_type = 'nirenfuku' AND o.is_final = 1 AND o.odds > 0
)
SELECT b.model_prob, m.pk,
       CASE WHEN EXISTS (SELECT 1 FROM payouts p
                          WHERE p.race_id = b.race_id
                            AND p.bet_type = 'nirenfuku'
                            AND p.combination = b.combination)
            THEN 1 ELSE 0 END AS y
  FROM bets b
  JOIN races r ON r.id = b.race_id
  JOIN mkt   m ON m.race_id = b.race_id AND m.combination = b.combination
 WHERE r.race_date = :d AND b.bet_type = 'nirenfuku'
   AND b.model_prob IS NOT NULL
"""
# created_at で絞らない理由:
# 損益を測るときは date(created_at) <= race_date が必須で、レース後の確定
# オッズで作られた買い目を外さないと「賭けていない金」が乗る。
# だがここで比べるのは model_prob と market_prob の精度だけで、
# **model_prob はオッズを一切見ずに出している**（出走表・モーター・調子）。
# 後から計算しても値は変わらないので、除くと測れる日が減るだけになる。
# 実際 08-16 はキャッチアップが再予測したため作成日が翌日になり、
# 絞ると 0 件になってその日の評価ができなかった。


def ll(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -(math.log(p) if y else math.log(1 - p))


def main() -> None:
    since = (sys.argv[sys.argv.index("--from") + 1]
             if "--from" in sys.argv else "2026-08-11")
    conn = sqlite3.connect(DB)
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT race_date FROM races WHERE race_date >= ? ORDER BY 1", (since,))]

    print("対数損失は小さいほど当たっている。差が正ならモデルの勝ち。\n")
    # 差は組合せごとに対で取る。集計値どうしを引くと、その差が誤差の
    # 範囲かどうかが分からない。
    print(f"{'日付':12}{'組合せ':>8}{'モデル':>9}{'市場':>9}{'差':>9}{'±2SE':>9}  判定")
    all_d: list[float] = []
    tot_m = tot_k = 0.0
    for d in days:
        rows = conn.execute(SQL, {"d": d}).fetchall()
        if len(rows) < 200:
            print(f"{d:12}{len(rows):>8}  （データ不足）")
            continue
        dm = [ll(r[1], r[2]) - ll(r[0], r[2]) for r in rows]   # 正=モデルの勝ち
        m = sum(ll(r[0], r[2]) for r in rows) / len(rows)
        k = sum(ll(r[1], r[2]) for r in rows) / len(rows)
        mean = sum(dm) / len(dm)
        sd = (sum((x - mean) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5
        se2 = 2 * sd / math.sqrt(len(dm))
        verdict = ("モデルが上" if mean - se2 > 0 else
                   "市場が上" if mean + se2 < 0 else "差なし")
        all_d += dm
        tot_m += m * len(rows); tot_k += k * len(rows)
        print(f"{d:12}{len(rows):>8}{m:>9.5f}{k:>9.5f}{mean:>+9.5f}{se2:>9.5f}  {verdict}")
    if all_d:
        n = len(all_d)
        mean = sum(all_d) / n
        sd = (sum((x - mean) ** 2 for x in all_d) / (n - 1)) ** 0.5
        se2 = 2 * sd / math.sqrt(n)
        verdict = ("モデルが上" if mean - se2 > 0 else
                   "市場が上" if mean + se2 < 0 else "差なし")
        print("-" * 65)
        print(f"{'合計':12}{n:>8}{tot_m / n:>9.5f}{tot_k / n:>9.5f}"
              f"{mean:>+9.5f}{se2:>9.5f}  {verdict}")
    print("\n※ 両方大きい日 = 荒れた日。モデルだけ大きい日 = モデルが外した日。")
    print("※ 対象は bets に model_prob があり、確定オッズが15通り揃ったレースのみ。")


if __name__ == "__main__":
    main()
