"""1号艇の人気薄バイアスは「買う時点のオッズ」でも成り立つのか。

なぜ必要か
--------
2026-08-30 に「1号艇の単勝はオッズが高いほど市場より安い」を見つけたが、
**あの測定は確定オッズで区分を切っていた**。買う時点では確定オッズは
分からない。同じ日に edge>=2.0 でまったく同じ罠を踏んでいる:

    確定オッズで区分  142%  →  締切前の板で区分  54%

締切前の板(board_*.json.gz)には単勝が入っていないので、代わりに
**朝の板(odds_raw_*.json.gz)** を使う。実運用は締切20分前に判定するので、
朝より条件は良い。つまりこれは**保守側の検証**で、ここで生き残れば本物に近い。

    選ぶ    朝の板の1号艇オッズ
    精算    確定オッズ（実際に払い戻される額）

使い方:
    python scripts/boat1_board_check.py
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
DB = ROOT / "data" / "boatrace.db"
KEEP = 0.736


def load():
    con = sqlite3.connect(DB)
    # 確定オッズ・着順・場コード
    fin, win, meta = {}, {}, {}
    for st, no, dt, o in con.execute("""
            SELECT s.code, r.race_no, r.race_date, o.odds
            FROM odds o JOIN races r ON r.id=o.race_id
            JOIN stadiums s ON s.id=r.stadium_id
            WHERE o.is_final=1 AND o.bet_type='tansho' AND o.combination='1' AND o.odds>0"""):
        fin[(st, int(no), dt)] = float(o)
    for st, no, dt, b in con.execute("""
            SELECT s.code, r.race_no, r.race_date, x.boat_no
            FROM race_results x JOIN races r ON r.id=x.race_id
            JOIN stadiums s ON s.id=r.stadium_id
            WHERE x.arrival_order=1"""):
        win[(st, int(no), dt)] = int(b)

    rows = []
    for p in sorted(glob.glob(str(DATA / "odds_raw_*.json.gz"))):
        day = os.path.basename(p)[9:19]
        d = json.loads(gzip.decompress(Path(p).read_bytes()).decode("utf-8"))
        for o in d.get("odds", []):
            if o.get("bet_type") != "tansho" or str(o.get("combination")) != "1":
                continue
            key = (str(o["stadium_code"]), int(o["race_no"]), day)
            f, w = fin.get(key), win.get(key)
            if f is None or w is None or not o.get("odds"):
                continue
            rows.append({"day": day, "board": float(o["odds"]), "final": f,
                         "hit": int(w == 1), "ret": f if w == 1 else 0.0})
    return rows


def stat(rows):
    n = len(rows)
    if not n:
        return 0, 0, 0, None, None
    hit = sum(x["hit"] for x in rows) / n * 100
    roi = sum(x["ret"] for x in rows) / n * 100
    if n < 30:
        return n, hit, roi, None, None
    random.seed(0)
    v = sorted(sum(s) / len(s) * 100 for s in
               ([random.choice(rows)["ret"] for _ in rows] for _ in range(2000)))
    return n, hit, roi, v[50], v[1949]


def show(label, rows):
    n, hit, roi, lo, hi = stat(rows)
    ci = f" [95% {lo:.0f}〜{hi:.0f}]" if lo is not None else ""
    star = "  ★" if lo is not None and lo > 100 else ""
    print(f"    {label:<16} {n:4d}本 的中{hit:5.1f}%  回収{roi:6.1f}%{ci}{star}")


def main():
    rows = load()
    if not rows:
        print("データが揃いませんでした")
        return
    days = sorted({x["day"] for x in rows})
    print("1号艇の単勝: **朝の板で選び、確定オッズで精算**")
    print(f"{len(days)}日 / {len(rows)}レース  ({days[0]} 〜 {days[-1]})")
    print(f"無作為の期待回収 {KEEP * 100:.1f}%。損益分岐 100%")
    print()

    print("=== 朝の板 → 確定オッズ の動き（1号艇）===")
    r = sorted(x["board"] / x["final"] for x in rows)
    print(f"    朝÷確定  中央値 {r[len(r) // 2]:.3f}  "
          f"（四分位 {r[len(r) // 4]:.3f} / {r[3 * len(r) // 4]:.3f}）")
    print("    1.0より大きい＝朝のほうが高い＝締切までに縮む")
    print()

    print("=== 朝の板のオッズで区分（実装できる形）===")
    for a, b in ((1.0, 1.3), (1.3, 1.6), (1.6, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 99)):
        show(f"{a:.1f}〜{b:.1f}倍", [x for x in rows if a <= x["board"] < b])
    print()
    print("=== 閾値以上をまとめて買う ===")
    for th in (1.6, 1.8, 2.0, 2.5, 3.0):
        show(f"{th:.1f}倍以上", [x for x in rows if x["board"] >= th])
    print()
    print("=== 比較: 確定オッズで区分した場合（買えない・参考値）===")
    for th in (2.0, 3.0):
        show(f"{th:.1f}倍以上", [x for x in rows if x["final"] >= th])
    print()
    print("=== 参考: 全レースで1号艇を買う ===")
    show("全部", rows)


if __name__ == "__main__":
    main()
