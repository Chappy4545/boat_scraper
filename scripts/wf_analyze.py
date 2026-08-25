"""walkforward.db を使って、母数不足で決められなかった問いに答える。

打ち切り(cutoff)ごとに別のモデル・別の期間なので、**cutoff を前半/後半に
割れば独立した2窓になる**。区分をたくさん試すので、必ず両窓で見る。

答える問い:
  Q1 惜しかったセル（オッズ1〜3倍 × edge 1.0〜1.2）は本物か
     2026-08-25 時点で 88% / 96%（母数不足で保留）
  Q2 本命寄りの偏り(+8pt)を切り分けて100%に届く区分はあるか
     場 / レース番号 / 何番人気か
  Q3 1点だけ買ったときの的中率はいくつか（精度重視モデルの出発点）

使い方:
    python scripts/wf_analyze.py
"""
from __future__ import annotations

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
    rows = con.execute("""
        SELECT cutoff, race_id, stadium, race_no, combination,
               model_prob, final_odds, hit FROM wf""").fetchall()
    races = defaultdict(list)
    for cu, rid, st, rno, cb, p, o, h in rows:
        races[(cu, rid)].append(
            {"cutoff": cu, "stadium": st, "race_no": rno, "combo": cb,
             "p": p, "odds": o, "hit": bool(h), "q": KEEP / o})
    return races


def roi(groups):
    k = sum(len(g) for g in groups)
    if not k:
        return 0.0, 0
    ret = sum(x["odds"] * 100 for g in groups for x in g if x["hit"])
    return ret / (k * 100) * 100, k


def boot(groups, T=800):
    if len(groups) < 20:
        return 0.0, 0.0
    random.seed(0)
    v = sorted(roi([random.choice(groups) for _ in groups])[0] for _ in range(T))
    return v[int(.025 * T)], v[int(.975 * T)]


def split_windows(races):
    cutoffs = sorted({k[0] for k in races})
    half = len(cutoffs) // 2 or 1
    a, b = set(cutoffs[:half]), set(cutoffs[half:])
    return ([v for k, v in races.items() if k[0] in a],
            [v for k, v in races.items() if k[0] in b], sorted(a), sorted(b))


def sel(groups, pred):
    out = []
    for g in groups:
        sub = [x for x in g if pred(x)]
        if sub:
            out.append(sub)
    return out


def show(label, ga, gb, note=""):
    ra, na = roi(ga)
    rb, nb = roi(gb)
    if na < 150 or nb < 150:
        print("  %-34s 母数不足 (%d / %d)" % (label, na, nb))
        return None
    la, ha = boot(ga)
    lb, hb = boot(gb)
    mark = " ★両窓100%超" if ra > 100 and rb > 100 else ""
    print("  %-34s %6.1f%% [%3.0f,%3.0f] (%5d) / %6.1f%% [%3.0f,%3.0f] (%5d)%s%s"
          % (label, ra, la, ha, na, rb, lb, hb, nb, mark, note))
    return ra > 100 and rb > 100


def main():
    if not DB.exists():
        raise SystemExit("walkforward.db がありません。先に walkforward.py を実行")
    races = load()
    ga, gb, ca, cb = split_windows(races)
    print("窓A cutoff=%s  %d レース" % (",".join(ca), len(ga)))
    print("窓B cutoff=%s  %d レース" % (",".join(cb), len(gb)))
    print()
    print("表の見方: 回収率 [95%区間] (組合せ数)   左=窓A / 右=窓B")
    hits = []

    print()
    print("=== Q1 惜しかったセル ===")
    for olo, ohi in [(1, 3), (3, 5)]:
        for elo, ehi in [(0.9, 1.1), (1.0, 1.2), (1.1, 1.3), (1.2, 1.5)]:
            f = lambda x, a=olo, b=ohi, c=elo, d=ehi: (
                a <= x["odds"] < b and c <= x["p"] / x["q"] < d)
            r = show("オッズ%d〜%d × edge %.1f〜%.1f" % (olo, ohi, elo, ehi),
                     sel(ga, f), sel(gb, f))
            if r:
                hits.append("オッズ%d〜%d × edge %.1f〜%.1f" % (olo, ohi, elo, ehi))

    print()
    print("=== Q2 本命寄りの偏りを切り分ける ===")
    print("-- オッズ帯だけ（モデル不使用）--")
    for olo, ohi in [(1, 2), (2, 3), (3, 5), (5, 10), (10, 30)]:
        f = lambda x, a=olo, b=ohi: a <= x["odds"] < b
        show("確定オッズ %d〜%d倍" % (olo, ohi), sel(ga, f), sel(gb, f))

    print("-- レース番号（1〜3R は初戦・10〜12R は準優/優勝戦が多い）--")
    for lo, hi in [(1, 4), (4, 8), (8, 11), (11, 13)]:
        f = lambda x, a=lo, b=hi: a <= x["race_no"] < b and 1 <= x["odds"] < 5
        show("R%d〜%d × オッズ1〜5倍" % (lo, hi - 1), sel(ga, f), sel(gb, f))

    print("-- 場ごと（オッズ1〜5倍に限定）--")
    st = sorted({x["stadium"] for g in ga for x in g})
    for s in st:
        f = lambda x, t=s: x["stadium"] == t and 1 <= x["odds"] < 5
        r = show(s, sel(ga, f), sel(gb, f))
        if r:
            hits.append("場: " + s)

    print()
    print("=== Q3 1点だけ買ったときの的中率（精度重視モデル）===")
    for label, key in (("モデルが一番高い1点", "p"),
                       ("市場が一番堅い1点", "q")):
        for w, g in (("窓A", ga), ("窓B", gb)):
            picks = [max(r, key=lambda x: x[key]) for r in g]
            n = len(picks)
            h = sum(1 for x in picks if x["hit"])
            ret = sum(x["odds"] * 100 for x in picks if x["hit"])
            print("  %-20s %s: %d本 的中%5.1f%% 回収%6.1f%% 平均オッズ%.2f"
                  % (label, w, n, h / n * 100, ret / (n * 100) * 100,
                     sum(x["odds"] for x in picks) / n))

    print()
    print("=== 両窓で100%を超えた区分 ===")
    print("  %s" % (hits if hits else "なし"))


if __name__ == "__main__":
    main()
