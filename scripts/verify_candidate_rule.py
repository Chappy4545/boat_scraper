"""CP1: 実装したルールが、測定した成績を再現するか。

ウォークフォワード（14,187レース・2窓）で
    モデルの1点 × edge>=2.0（確定オッズ）→ 142.1% / 118.2%
と出た。src/betting/candidate_rule.py の実装に**確定オッズを直接渡して**
同じ数字が出るか確かめる。ずれたら実装が仕様と違う。

そのうえで「板から推定した確定オッズ」を使うとどうなるかも並べる
（板の在庫が54レースしかないので参考値）。

使い方:
    python scripts/verify_candidate_rule.py
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.betting.candidate_rule import (          # noqa: E402
    pick_top1, edge_of, TAKEOUT_KEEP,
)

DB = Path("data/processed/walkforward.db")
MIN_EDGE = 2.0


def load():
    con = sqlite3.connect(DB)
    races = defaultdict(list)
    for cu, rid, cb, p, o, h in con.execute(
            "SELECT cutoff, race_id, combination, model_prob, final_odds, hit FROM wf"):
        races[(cu, rid)].append({"combination": cb, "model_prob": p,
                                 "odds": o, "hit": bool(h)})
    return races


def run(races, keys):
    picks = []
    for k in keys:
        combos = races[k]
        top = pick_top1(combos)
        if top is None:
            continue
        # CP1 は確定オッズをそのまま使う（測定と同じ土俵にする）
        if edge_of(top["model_prob"], top["odds"]) >= MIN_EDGE:
            picks.append(top)
    return picks


def stat(picks):
    n = len(picks)
    if not n:
        return 0, 0.0, 0.0
    h = sum(1 for x in picks if x["hit"])
    ret = sum(x["odds"] * 100 for x in picks if x["hit"])
    return n, h / n * 100, ret / (n * 100) * 100


def boot(picks, T=1000):
    if len(picks) < 50:
        return 0.0, 0.0
    random.seed(0)
    v = sorted(stat([random.choice(picks) for _ in picks])[2] for _ in range(T))
    return v[int(.025 * T)], v[int(.975 * T)]


def main():
    races = load()
    cutoffs = sorted({k[0] for k in races})
    half = len(cutoffs) // 2
    A = [k for k in races if k[0] in set(cutoffs[:half])]
    B = [k for k in races if k[0] in set(cutoffs[half:])]

    print("=== CP1: 実装 × 確定オッズ（測定と同じ土俵）===")
    print("窓         本数  的中率   回収率  95%区間")
    ok = True
    for label, keys, want in (("窓A", A, 142.1), ("窓B", B, 118.2)):
        picks = run(races, keys)
        n, hr, roi = stat(picks)
        lo, hi = boot(picks)
        gap = abs(roi - want)
        print("  %-6s %5d %6.1f%% %7.1f%% [%.0f, %.0f]   測定値 %.1f%% との差 %.1f"
              % (label, n, hr, roi, lo, hi, want, gap))
        if gap > 1.0:
            ok = False
    print()
    print("判定: %s" % ("OK 実装は測定を再現している"
                       if ok else "★NG 実装が仕様とずれている"))

    print()
    print("=== 参考: このルールが選ぶ買い目の姿 ===")
    picks = run(races, A + B)
    n = len(picks)
    print("  全期間 %d本 / %d レース中 = %.1f%%" % (n, len(races), n / len(races) * 100))
    print("  平均オッズ %.2f倍 / 平均モデル確率 %.3f"
          % (sum(x["odds"] for x in picks) / n,
             sum(x["model_prob"] for x in picks) / n))
    od = sorted(x["odds"] for x in picks)
    print("  オッズ 四分位 %.1f / %.1f / %.1f 倍"
          % (od[n // 4], od[n // 2], od[n * 3 // 4]))
    print("  ※ 1日150レースなら 1日あたり約 %.0f 本" % (n / len(races) * 150))


if __name__ == "__main__":
    main()
