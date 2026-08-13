"""過去のペーパー記録に的中/外れを埋める。

cmd_judge が is_pass == False で絞っていたため、見送った買い目は保存される
だけで一度も判定されていなかった（2026-08-13 に修正）。その積み残しが
40 万件あり、賭式の比較にも閾値の再検証にもそのままでは使えない。

cmd_judge を日ごとに呼ぶと 1 日ぶんごとに export が走って遅いので、
ここでは判定だけを一括で行い、export は呼ばない（必要なら後で1回流す）。

使い方:
    python scripts/backfill_judge.py            # 実行
    python scripts/backfill_judge.py --dry-run  # 件数だけ数える
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "boatrace.db"

# 判定対象: まだ is_hit が入っておらず、当日オッズが記録されていて、
# そのレースの着順が存在するもの。着順が無いレースは不成立や取り逃しで、
# 判定しようがない（返還なので外れ扱いにしてはいけない）。
TARGET = """
    SELECT b.id, b.race_id, b.bet_type, b.combination
      FROM bets b
     WHERE b.is_hit IS NULL
       AND b.odds IS NOT NULL
       AND EXISTS (SELECT 1 FROM race_results rr WHERE rr.race_id = b.race_id)
"""


def main() -> None:
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")

    t0 = time.time()
    rows = conn.execute(TARGET).fetchall()
    print(f"判定対象: {len(rows):,}件 ({time.time() - t0:.1f}秒)")
    if dry or not rows:
        return

    # 払戻を (race_id, bet_type, combination) -> payout で一括に持つ。
    # 1件ずつ引くと 40 万回のクエリになる。
    t0 = time.time()
    payouts = {
        (rid, bt, comb): pay
        for rid, bt, comb, pay in conn.execute(
            "SELECT race_id, bet_type, combination, payout FROM payouts"
        )
    }
    print(f"払戻の読み込み: {len(payouts):,}件 ({time.time() - t0:.1f}秒)")

    t0 = time.time()
    updates = [
        (1 if (pay := payouts.get((rid, bt, comb))) is not None else 0, pay, bid)
        for bid, rid, bt, comb in rows
    ]
    conn.executemany(
        "UPDATE bets SET is_hit = ?, actual_payout = ? WHERE id = ?", updates
    )
    conn.commit()
    hits = sum(u[0] for u in updates)
    print(f"判定完了: {len(updates):,}件 (的中 {hits:,}件 "
          f"= {hits / len(updates) * 100:.1f}%) ({time.time() - t0:.1f}秒)")


if __name__ == "__main__":
    main()
