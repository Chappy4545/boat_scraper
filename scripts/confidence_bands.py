"""「どれが堅いか」を的中率で測る。

なぜ的中率で見るのか
--------------------
回収率は配当のばらつきが大きく、17,000レースでも区間が±5〜15pt になる。
一方**的中率は同じ本数で±1〜2pt** に収まる。つまり
「勝てるか（回収率）」は測りにくいが、「**当たりやすいか（的中率）**」は
はっきり測れる。画面で「どれが堅いか」を示すにはこちらを使う。

⚠️ 的中率が高い＝勝てる、ではない。的中率が上がるぶん配当は下がるので、
回収率は別問題（[[project_bet_types_six]]）。表示では必ず分けて書く。

データ
------
`wf_holdout.db`（2026-02-02〜04-22・**探索に一度も使っていない**）を主に使い、
`wf_picks.db`（05-02〜08-29）でも同じ帯を出して、ズレていないかを見る。
両方で同じなら、帯の切り方は期間に依らない。

使い方
------
    python scripts/confidence_bands.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
HOLDOUT = ROOT / "data" / "processed" / "wf_holdout.db"
EXPLORE = ROOT / "data" / "processed" / "wf_picks.db"

TYPES = ["fukusho", "tansho", "kakurenfuku",
         "nirenfuku", "sanrenfuku", "sanrentan"]
JP = {"fukusho": "複勝", "tansho": "単勝", "kakurenfuku": "拡連複",
      "nirenfuku": "2連複", "sanrenfuku": "3連複", "sanrentan": "3連単"}
# 確率の四分位で4段階に分ける。段階の名前は「堅さ」を表す。
GRADES = ["C", "B", "A", "S"]


def load(db, bt):
    con = sqlite3.connect(db)
    rows = [(float(p), int(h), float(r)) for p, h, r in con.execute(
        "SELECT model_prob, hit, ret FROM picks "
        "WHERE bet_type=? AND model_prob IS NOT NULL AND ret IS NOT NULL", (bt,))]
    con.close()
    return rows


def wilson(k, n):
    """的中率の95%区間（Wilson）。件数が少なくても暴れない。"""
    if n == 0:
        return (float("nan"), float("nan"))
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0, c - h), min(1, c + h))


def main():
    if not HOLDOUT.exists():
        print(f"{HOLDOUT} がありません")
        return
    print("確率の四分位で4段階に分け、未使用データで的中率を測る")
    print("（S=最上位25% … C=最下位25%）\n")
    out = {}
    for bt in TYPES:
        h = load(HOLDOUT, bt)
        if len(h) < 400:
            continue
        probs = np.array([r[0] for r in h])
        cuts = np.percentile(probs, [25, 50, 75])
        print(f"[{JP[bt]}]  未使用 {len(h):,}本 / 探索 {len(load(EXPLORE, bt)):,}本")
        print(f"  {'段':3} {'確率の範囲':>16} {'的中率(未使用)':>20} "
              f"{'的中率(探索)':>14} {'回収率':>8}")
        bands = []
        e = load(EXPLORE, bt)
        eprobs = np.array([r[0] for r in e])
        for i, g in enumerate(GRADES):
            lo = cuts[i - 1] if i > 0 else 0.0
            hi = cuts[i] if i < 3 else 1.01
            sub = [r for r in h if lo <= r[0] < hi]
            esub = [r for r in e if lo <= r[0] < hi]
            if not sub:
                continue
            k, n = sum(r[1] for r in sub), len(sub)
            cl, ch = wilson(k, n)
            roi = float(np.mean([r[2] for r in sub]))
            ek = sum(r[1] for r in esub)
            ehr = ek / len(esub) if esub else float("nan")
            print(f"  {g:3} {lo:6.3f}-{hi:6.3f} "
                  f"{k/n*100:9.1f}% [{cl*100:5.1f}-{ch*100:5.1f}] {n:6,}本"
                  f" {ehr*100:11.1f}% {roi*100:7.1f}%")
            bands.append((g, round(lo, 3), round(k / n, 4)))
        out[bt] = (round(float(cuts[0]), 3), round(float(cuts[1]), 3),
                   round(float(cuts[2]), 3), bands)
        print()

    print("=" * 66)
    print("画面に載せる形（app.js の CONFIDENCE へ）")
    for bt, (c1, c2, c3, bands) in out.items():
        hr = {g: p for g, _lo, p in bands}
        print(f"  {bt:12}: {{ cuts: [{c1}, {c2}, {c3}], "
              f"hit: [{hr.get('C',0):.3f}, {hr.get('B',0):.3f}, "
              f"{hr.get('A',0):.3f}, {hr.get('S',0):.3f}] }},")


if __name__ == "__main__":
    main()
