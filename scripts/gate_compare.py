"""買い目を通す条件（ゲート）を、買える形で比べる。

現行ルールと案は**出発点が同じ**
--------------------------------
本番は賭式ごとに「モデルの確率が最大の1点」を選び、そのあと条件で
通すか止めるかを決める（main.py の best_of_type → overrides）。
つまり選ぶ1点は同じで、**違うのは通す条件だけ**。だから同一レースで
そのまま比べられる。

    現行 r5      確率>=0.30 かつ EV>=1.2
    案A          確率>=0.435 かつ EV>=1.2   （確率の下限を上げる）
    案B          確率>=0.435 のみ           （EVの条件を外す）
    案C          EV条件なし・確率条件なし    （全部買う＝基準線）

⚠️ EV は**締切前の板**で計算する
--------------------------------
確定オッズで EV を作ると「レース後にしか分からない値で選ぶ」ことになり、
必ず良く見える（2026-08-30 に3回この罠にかかった）。ここでは
`docs/data/board_<日付>.json.gz`（締切20分前に取得した板）だけを使う。

⚠️ 汚染について
---------------
確率の閾値 0.435 は 05-02〜08-29 から選んだ。この測定はその一部
（板がある 08-21〜08-29）なので、**案A/Bに有利**に出る。
未使用データでの確認では効果は半分に縮んだ（[[project_prob_filter_works]]）。
ここで見たいのは「現行より良いか」の**向き**であって、幅ではない。

使い方
------
    python scripts/gate_compare.py
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "docs" / "data"
PICKS = ROOT / "data" / "processed" / "wf_picks.db"

BET = "nirenfuku"          # 本番で賭け金を付けている唯一の賭式
MIN_ODDS, MAX_ODDS = 1.5, 50.0


def load_board():
    """締切前の板。(日付,場,R,組) -> オッズ"""
    stad = json.loads((DATA / "stadiums.json").read_text(encoding="utf-8"))
    code_of = {s["name"]: str(s["code"]) for s in stad}
    name_of = {v: k for k, v in code_of.items()}
    out = {}
    for p in sorted(glob.glob(str(DATA / "board_*.json.gz"))):
        d = os.path.basename(p)[6:16]
        j = json.load(gzip.open(p, "rt", encoding="utf-8"))
        for _rid, r in j.get("races", {}).items():
            st = r.get("stadium")
            if st not in code_of:
                st = name_of.get(st, st)
            for o in r.get("odds", []):
                if o["bet_type"] == BET and o.get("odds"):
                    out[(d, st, int(r["race_no"]), o["combination"])] = float(o["odds"])
    return out


def main():
    board = load_board()
    con = sqlite3.connect(PICKS)
    rows = []
    for rd, st, rn, comb, p, ret in con.execute(
            "SELECT race_date, stadium, race_no, combination, model_prob, ret "
            "FROM picks WHERE bet_type=? AND ret IS NOT NULL", (BET,)):
        od = board.get((rd, st, int(rn), comb))
        if not od:
            continue
        rows.append({"race": (rd, st, rn), "prob": float(p), "odds": od,
                     "ev": float(p) * od, "ret": float(ret), "date": rd})
    con.close()

    if not rows:
        print("板と予測が両方そろうレースがありません")
        return
    days = sorted({r["date"] for r in rows})
    print(f"締切前の板と予測が両方ある: {len(rows):,}レース  {days[0]}〜{days[-1]}")
    print("⚠️ 確率の閾値はこの期間を含むデータから選んだので、案A/Bに有利に出る\n")

    def gate(name, fn):
        sel = [r for r in rows if fn(r)]
        if not sel:
            print(f"  {name:34} 該当なし")
            return
        n = len(sel)
        roi = float(np.mean([r["ret"] for r in sel]))
        hit = float(np.mean([r["ret"] > 0 for r in sel]))
        # レース単位＝1レース1本なので行単位のブートストラップでよい
        rng = np.random.default_rng(0)
        vals = np.array([r["ret"] for r in sel])
        bs = [vals[rng.integers(0, n, n)].mean() for _ in range(3000)]
        lo, hi = np.percentile(bs, [2.5, 97.5])
        print(f"  {name:34} {roi*100:6.1f}% [{lo*100:5.1f}〜{hi*100:5.1f}]"
              f"  {n:5,}本  的中{hit*100:5.1f}%  1日{n/len(days):5.1f}本")

    okodds = lambda r: MIN_ODDS <= r["odds"] <= MAX_ODDS   # noqa: E731
    print("2連複・確率が最大の1点。通す条件だけを変える")
    gate("C 全部買う（基準線）", lambda r: True)
    gate("  オッズ 1.5-50 のみ", okodds)
    gate("現行 r5  確率>=0.30 かつ EV>=1.2",
         lambda r: okodds(r) and r["prob"] >= 0.30 and r["ev"] >= 1.2)
    gate("案A     確率>=0.435 かつ EV>=1.2",
         lambda r: okodds(r) and r["prob"] >= 0.435 and r["ev"] >= 1.2)
    gate("案B     確率>=0.435 のみ",
         lambda r: okodds(r) and r["prob"] >= 0.435)
    print()
    print("参考: EVの条件だけを動かす（確率>=0.30 は固定）")
    for ev in (1.0, 1.2, 1.5, 2.0):
        gate(f"        確率>=0.30 かつ EV>={ev}",
             lambda r, e=ev: okodds(r) and r["prob"] >= 0.30 and r["ev"] >= e)
    print()
    print("参考: 確率の条件だけを動かす（EVの条件なし）")
    for pr in (0.30, 0.35, 0.40, 0.435, 0.50):
        gate(f"        確率>={pr}（EV条件なし）",
             lambda r, q=pr: okodds(r) and r["prob"] >= q)


if __name__ == "__main__":
    main()
