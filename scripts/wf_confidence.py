"""「モデルの1点」をどう絞れば回収率が上がるかを探す。

Q3 で分かったこと（14,187レース・2窓）:
    モデルの1点   的中32.7/32.4%  回収 91.2/83.8%  平均オッズ3.30/3.01
    市場の1点     的中32.7/33.0%  回収 75.4/75.4%  平均オッズ2.54/2.50
  → 精度は市場と同じ。価値（同じ当たりやすさをより高い配当で拾う）で
     10〜15pt 勝っている。全レース買って91%。

ここでは「どのレースを買わないか」を探す。

⚠️ 実際に使えるか（実装可能性）で2つに分ける:
  【使える】 モデルの出力だけで決まる条件（確率・1位と2位の差・分布の尖り）
  【上限】   確定オッズを使う条件（edge など）。買う時点では確定オッズを
            知り得ないので、そのままは実現できない。上限の目安として見る。

使い方:
    python scripts/wf_confidence.py
"""
from __future__ import annotations

import math
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

DB = Path("data/processed/walkforward.db")
KEEP = 0.742


def load():
    con = sqlite3.connect(DB)
    races = defaultdict(list)
    for cu, rid, cb, p, o, h in con.execute(
            "SELECT cutoff, race_id, combination, model_prob, final_odds, hit FROM wf"):
        races[(cu, rid)].append({"combo": cb, "p": p, "odds": o,
                                 "hit": bool(h), "q": KEEP / o})
    out = []
    for (cu, rid), rows in races.items():
        rows.sort(key=lambda x: -x["p"])
        top = rows[0]
        out.append({
            "cutoff": cu,
            "top": top,
            "p": top["p"],
            "gap": top["p"] - rows[1]["p"] if len(rows) > 1 else top["p"],
            "edge": top["p"] / top["q"] if top["q"] else 0,
            # 分布の尖り（小さいほど1点に集中している）
            "ent": -sum(x["p"] * math.log(max(x["p"], 1e-9)) for x in rows),
            "top3": rows[:3],
        })
    return out


def stat(picks):
    n = len(picks)
    if not n:
        return 0, 0, 0
    h = sum(1 for x in picks if x["top"]["hit"])
    ret = sum(x["top"]["odds"] * 100 for x in picks if x["top"]["hit"])
    return n, h / n * 100, ret / (n * 100) * 100


def boot(picks, T=800):
    if len(picks) < 50:
        return 0, 0
    random.seed(0)
    v = sorted(stat([random.choice(picks) for _ in picks])[2] for _ in range(T))
    return v[int(.025 * T)], v[int(.975 * T)]


def report(label, a, b, note=""):
    na, ha, ra = stat(a)
    nb, hb, rb = stat(b)
    if na < 200 or nb < 200:
        print("  %-30s 母数不足 (%d/%d)" % (label, na, nb))
        return False
    la, _ = boot(a)
    lb, _ = boot(b)
    mark = "  ★両窓100%超" if ra > 100 and rb > 100 else ""
    print("  %-30s %5.1f%%(下限%3.0f) 的中%4.1f%% n=%4d / %5.1f%%(下限%3.0f) 的中%4.1f%% n=%4d%s%s"
          % (label, ra, la, ha, na, rb, lb, hb, nb, mark, note))
    return ra > 100 and rb > 100


def main():
    races = load()
    cutoffs = sorted({r["cutoff"] for r in races})
    half = len(cutoffs) // 2
    A = [r for r in races if r["cutoff"] in set(cutoffs[:half])]
    B = [r for r in races if r["cutoff"] in set(cutoffs[half:])]
    print("窓A %d レース / 窓B %d レース" % (len(A), len(B)))
    print("表: 回収率(95%下限) 的中率 本数   左=窓A / 右=窓B")
    found = []

    print()
    print("=== 【使える】モデルの確率で絞る ===")
    for lo, hi in [(0, .25), (.25, .3), (.3, .35), (.35, .4), (.4, .5), (.5, 1)]:
        f = lambda r, a=lo, b=hi: a <= r["p"] < b
        if report("p %.2f〜%.2f" % (lo, hi), [r for r in A if f(r)], [r for r in B if f(r)]):
            found.append("p %.2f〜%.2f" % (lo, hi))

    print()
    print("=== 【使える】1位と2位の差で絞る（差が大きい＝迷いがない）===")
    for lo, hi in [(0, .05), (.05, .1), (.1, .15), (.15, .25), (.25, 1)]:
        f = lambda r, a=lo, b=hi: a <= r["gap"] < b
        if report("gap %.2f〜%.2f" % (lo, hi), [r for r in A if f(r)], [r for r in B if f(r)]):
            found.append("gap %.2f〜%.2f" % (lo, hi))

    print()
    print("=== 【使える】上位2点・3点を買う ===")
    for k in (2, 3):
        def st(picks, kk=k):
            n = len(picks) * kk
            h = sum(1 for r in picks for x in r["top3"][:kk] if x["hit"])
            ret = sum(x["odds"] * 100 for r in picks for x in r["top3"][:kk] if x["hit"])
            return n, h / n * 100, ret / (n * 100) * 100
        na, ha, ra = st(A)
        nb, hb, rb = st(B)
        print("  %-30s %5.1f%% 的中%4.1f%% n=%5d / %5.1f%% 的中%4.1f%% n=%5d"
              % ("上位%d点を毎レース" % k, ra, ha, na, rb, hb, nb))

    print()
    print("=== 【上限】確定オッズを使う条件（そのままは実現できない）===")
    for lo, hi in [(1.0, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, 99)]:
        f = lambda r, a=lo, b=hi: a <= r["edge"] < b
        report("edge %.1f〜%.1f" % (lo, hi), [r for r in A if f(r)], [r for r in B if f(r)],
               note="  ※上限")
    for lo, hi in [(2, 3), (3, 4), (4, 6), (6, 10)]:
        f = lambda r, a=lo, b=hi: a <= r["top"]["odds"] < b
        report("確定オッズ %d〜%d倍" % (lo, hi),
               [r for r in A if f(r)], [r for r in B if f(r)], note="  ※上限")

    print()
    print("=== 両窓で100%を超えた条件 ===")
    print("  %s" % (found if found else "なし"))


if __name__ == "__main__":
    main()
