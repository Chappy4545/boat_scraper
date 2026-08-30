"""「edge>=2.0 の1点買い」は、締切前の情報だけで本当に成立するのか。

なぜこれを測るか
--------------
ウォークフォワード（14,187レース・2窓）で、実装可能な条件のうち唯一
両窓とも100%を超えたのがこれだった:

    モデルの1点 × edge>=2.0    窓A 142.1%[下限120] / 窓B 118.2%[下限97]

**ただしこれは確定オッズで計算した値**で、買う時点では知り得ない。
そこで板から確定オッズを見込む候補ルール(top1_value)を回したが、
推定/確定の中央値が 1.44（60本）と大きく過大評価していた。

142% が「本物の妙味」なのか「確定オッズを先に知っていたから出た幻」なのかは、
**締切前の板だけを使って同じルールを回せば**決着する。それがこの検証。

使うもの（すべて買う時点で手に入るもの）:
    docs/data/board_<日>.json.gz  締切前の板（中央値14分前・全通り）
    docs/data/probs_<日>.json     その朝のモデル確率（当日の再訓練前＝未見データ）
成績の答え合わせだけ DB の着順・払戻を使う。

使い方:
    python scripts/board_edge_reality.py
"""
from __future__ import annotations

import glob
import gzip
import json
import math
import os
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
DB = ROOT / "data" / "boatrace.db"
KEEP = 0.742          # 2連複の控除後（実測 sum(1/確定オッズ)=1.348）
BET = "nirenfuku"
# 候補ルールが使っている縮み補正（全15通りで推定したもの）
COEF = (0.2910, 0.5527, 0.2899)


def code_to_name():
    st = json.loads((DATA / "stadiums.json").read_text(encoding="utf-8"))
    return {s["code"]: s["name"] for s in st}


def load_probs(day, c2n):
    """(場, レース番号) -> {組: モデル確率}"""
    p = DATA / f"probs_{day}.json"
    if not p.exists():
        return {}
    out = {}
    for e in json.loads(p.read_text(encoding="utf-8")).get("races", []):
        key = (c2n.get(e.get("stadium_code")), e.get("race_no"))
        combos = {c["combination"]: c["model_prob"]
                  for c in e.get("combinations", []) if c.get("bet_type") == BET}
        if combos:
            out[key] = combos
    return out


def load_board(day):
    """(場, レース番号) -> {組: 板オッズ}"""
    p = DATA / f"board_{day}.json.gz"
    if not p.exists():
        return {}
    d = json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
    out = {}
    for _rid, v in d.get("races", {}).items():
        combos = {o["combination"]: o["odds"] for o in v.get("odds", [])
                  if o.get("bet_type") == BET and (o.get("odds") or 0) > 0}
        if combos:
            out[(v.get("stadium"), v.get("race_no"))] = combos
    return out


def load_truth(days):
    """(場, レース番号, 日) -> (勝ち組, 払戻/100)"""
    con = sqlite3.connect(DB)
    top2, pay = {}, {}
    qs = ",".join("?" * len(days))
    for st, no, dt, order, boat in con.execute(f"""
            SELECT s.name, r.race_no, r.race_date, x.arrival_order, x.boat_no
            FROM race_results x JOIN races r ON r.id=x.race_id
            JOIN stadiums s ON s.id=r.stadium_id
            WHERE r.race_date IN ({qs}) AND x.arrival_order IN (1,2)""", days):
        if boat:
            top2.setdefault((st, no, dt), []).append(int(boat))
    for st, no, dt, comb, p in con.execute(f"""
            SELECT s.name, r.race_no, r.race_date, p.combination, p.payout
            FROM payouts p JOIN races r ON r.id=p.race_id
            JOIN stadiums s ON s.id=r.stadium_id
            WHERE r.race_date IN ({qs}) AND p.bet_type=?""", (*days, BET)):
        pay[(st, no, dt)] = (comb, (p or 0) / 100.0)
    return top2, pay


def adjusted(board, p):
    a, b, c = COEF
    return math.exp(a + b * math.log(board) + c * math.log(1.0 / p))


def boot(rows, T=2000):
    if len(rows) < 30:
        return None, None
    random.seed(0)
    v = []
    for _ in range(T):
        s = [random.choice(rows) for _ in rows]
        v.append(sum(x["ret"] for x in s) / len(s) * 100)
    v.sort()
    return v[int(.025 * T)], v[int(.975 * T)]


def show(label, rows):
    n = len(rows)
    if not n:
        print(f"  {label:<26} 0本")
        return
    hits = sum(1 for x in rows if x["ret"] > 0)
    roi = sum(x["ret"] for x in rows) / n * 100
    lo, hi = boot(rows)
    ci = f" [95% {lo:.0f}〜{hi:.0f}]" if lo is not None else " (母数不足)"
    mark = "  ★100%超" if lo is not None and lo > 100 else ""
    print(f"  {label:<26} {n:4d}本 的中{hits / n * 100:4.1f}%  回収{roi:6.1f}%{ci}{mark}")


def main():
    c2n = code_to_name()
    days = sorted(os.path.basename(f)[6:16] for f in glob.glob(str(DATA / "board_*.json.gz")))
    top2, pay = load_truth(days)

    picks = []
    for day in days:
        probs, board = load_probs(day, c2n), load_board(day)
        for key, combos in board.items():
            mp = probs.get(key)
            if not mp:
                continue
            # ① 確率が最大の1点。オッズは見ない（top1_value と同じ選び方）
            comb = max(mp, key=mp.get)
            p, od = mp[comb], combos.get(comb)
            if not od or od <= 0:
                continue
            t = top2.get((*key, day))
            if not t or len(t) < 2:
                continue                      # 中止・欠場など
            won = set(map(int, comb.split("-"))) == set(t)
            got = pay.get((*key, day))
            ret = got[1] if (won and got and got[0] == comb) else 0.0
            picks.append({
                "day": day, "p": p, "board": od, "ret": ret,
                "edge_board": p * od / KEEP,                 # 板オッズそのまま
                "edge_adj": p * adjusted(od, p) / KEEP,      # 縮み補正あり
            })

    if not picks:
        print("データが揃いませんでした")
        return

    print(f"締切前の板がある {len(days)}日 / モデルの1点が取れた {len(picks)}レース")
    print("（板は締切の中央値14分前。モデル確率はその朝のもの＝当日の再訓練前）")
    print()
    print("■ 比較の基準")
    show("全部買う", picks)
    print(f"  {'無作為に買った場合':<26}          期待回収  74.2%（控除率25.8%）")

    print()
    print("■ 板オッズそのままで edge を作って絞る（実装可能）")
    for lo in (1.0, 1.5, 2.0, 2.5, 3.0):
        show(f"edge(板) >= {lo}", [x for x in picks if x["edge_board"] >= lo])

    print()
    print("■ 縮み補正を入れて絞る（候補ルール top1_value と同じ）")
    for lo in (1.2, 1.5, 2.0, 2.5):
        show(f"edge(補正) >= {lo}", [x for x in picks if x["edge_adj"] >= lo])

    print()
    print("■ 板オッズの帯で絞る")
    for a, b in ((1, 3), (3, 5), (5, 8), (8, 99)):
        show(f"板 {a}〜{b}倍", [x for x in picks if a <= x["board"] < b])

    print()
    print("■ モデル確率の帯で絞る")
    for a, b in ((0, .3), (.3, .4), (.4, .5), (.5, 1)):
        show(f"確率 {a:.1f}〜{b:.1f}", [x for x in picks if a <= x["p"] < b])


if __name__ == "__main__":
    main()
