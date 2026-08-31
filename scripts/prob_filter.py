"""「確率で絞ると良くなるか」を賭式ごと・2窓で測る。

なぜ要るか
----------
画面が推奨買い目を812件出している（144レース×6賭式の総当たり）。
実際に買うぶんだけに絞りたいが、**どう絞るかは測ってから決める**。

分かっていること:
  ・EV で絞ると**悪化する**（2026-08-30 実測。EV>=2.0 で 54.1%）
  ・賭式には順序がある（複勝 93.5% > … > 3連単 79.4%）
  ・どれも 100% 未満

まだ測っていないのが「**モデルの確率**で絞るとどうなるか」。
EV はオッズを含むので選択バイアスを拾うが、確率はオッズを一切見ない。
別物なので、悪化するとは限らない。

データ
------
`data/processed/wf_picks.db`（17,090レース・**オッズを入れていない**）。
各レース・各賭式について「モデルの確率が最大の1点」と、その払戻。
前半で探し、後半で確かめる。誤差はレース単位のブートストラップ。

使い方
------
    python scripts/prob_filter.py
    python scripts/prob_filter.py --types fukusho,tansho
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "processed" / "wf_picks.db"

# 賭式ごとの実測回収率（悪い方の窓）。並びは良い順。
TIER_ORDER = ["fukusho", "tansho", "kakurenfuku",
              "nirenfuku", "sanrenfuku", "sanrentan"]


def roi(rows):
    return float(np.mean([r[1] for r in rows])) if rows else float("nan")


def boot(rows, n=1500, seed=0):
    """レース単位で再抽出する。同じレースの行は連動するため。"""
    if len(rows) < 30:
        return (float("nan"), float("nan"))
    by = defaultdict(list)
    for key, ret in rows:
        by[key].append((key, ret))
    races = list(by.values())
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        idx = rng.integers(0, len(races), len(races))
        s = [x for i in idx for x in races[i]]
        out.append(roi(s))
    return (float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5)))


def load(types):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = defaultdict(list)          # bet_type -> [(race_key, model_prob, ret, date)]
    for r in con.execute(
            "SELECT race_date, stadium, race_no, bet_type, model_prob, ret "
            "FROM picks WHERE ret IS NOT NULL AND model_prob IS NOT NULL"):
        if r["bet_type"] not in types:
            continue
        rows[r["bet_type"]].append(
            ((r["race_date"], r["stadium"], r["race_no"]),
             float(r["model_prob"]), float(r["ret"]), r["race_date"]))
    con.close()
    return rows


def bands_for(vals):
    """確率の分布を5等分する。賭式ごとに確率の水準が全く違うため、
    固定の区切りにすると片方の賭式で全部が1つの箱に入る。"""
    qs = np.percentile(vals, [20, 40, 60, 80])
    return [(None, qs[0]), (qs[0], qs[1]), (qs[1], qs[2]),
            (qs[2], qs[3]), (qs[3], None)]


def report(bt, rows):
    dates = sorted({r[3] for r in rows})
    half = len(dates) // 2
    wins = {d: ("A" if i < half else "B") for i, d in enumerate(dates)}
    probs = [r[1] for r in rows]
    bands = bands_for(probs)

    print(f"\n[{bt}]  {len(rows):,}点 / {len({r[0] for r in rows}):,}レース")
    base = {}
    for w in ("A", "B"):
        sub = [(r[0], r[2]) for r in rows if wins[r[3]] == w]
        base[w] = roi(sub)
    print(f"  全部買う           窓A {base['A']*100:6.1f}%   窓B {base['B']*100:6.1f}%")
    print(f"  {'確率帯':22} {'窓A':>22} {'窓B':>22}")
    for lo, hi in bands:
        cells, ok = [], True
        for w in ("A", "B"):
            sub = [(r[0], r[2]) for r in rows
                   if wins[r[3]] == w
                   and (lo is None or r[1] >= lo) and (hi is None or r[1] < hi)]
            if len(sub) < 30:
                cells.append(f"{'不足':>22}")
                ok = False
                continue
            v = roi(sub)
            cl, ch = boot(sub)
            cells.append(f"{v*100:6.1f}% [{cl*100:5.1f}-{ch*100:5.1f}]")
        label = (f"{(lo or 0):.3f}-{hi:.3f}" if hi is not None
                 else f"{lo:.3f} 以上")
        star = ""
        if ok:
            # 両窓とも「全部買う」を上回ったときだけ印をつける
            a = roi([(r[0], r[2]) for r in rows if wins[r[3]] == "A"
                     and (lo is None or r[1] >= lo) and (hi is None or r[1] < hi)])
            b = roi([(r[0], r[2]) for r in rows if wins[r[3]] == "B"
                     and (lo is None or r[1] >= lo) and (hi is None or r[1] < hi)])
            if a > base["A"] and b > base["B"]:
                star = "  ← 両窓で改善"
        print(f"  {label:22} {cells[0]} {cells[1]}{star}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", default=",".join(TIER_ORDER))
    a = ap.parse_args()
    if not DB.exists():
        print(f"{DB} がありません。先に scripts/wf_store.py を回してください")
        return
    rows = load(set(a.types.split(",")))
    print("確率で絞ると回収率が上がるか（前半で探し、後半で確かめる）")
    print("⚠️ EV で絞ると悪化することは既に分かっている。ここで見るのは確率。")
    for bt in TIER_ORDER:
        if bt in rows and len(rows[bt]) >= 300:
            report(bt, rows[bt])


if __name__ == "__main__":
    main()
