"""確率で絞る買い方を、一度も使っていないデータで確かめる。

⚠️ 事前登録（結果を見る前に書いた・2026-08-31）
================================================
2026-08-31 に「モデルの確率で絞ると回収率が上がる」を見つけたが、
**その条件は 05-02〜08-29 の両方の窓を使って選んだ**。同じデータで
確かめても意味がない（選んだ場所で測っているだけ）。

そこで一度も触っていない **2026-02〜04** で確かめる。
（`wf_store.py --out data/processed/wf_holdout.db --cutoffs
  2026-02-01,2026-02-21,2026-03-13,2026-04-02`
  予測期間は 02-02〜04-22。wf_picks の 05-02 と重ならない）

検証する仮説（これ以外は見ない）
--------------------------------
本番に載せた閾値そのもので測る。「上位20%」という規則ではなく、
**画面で実際に使っている絶対値**が本番の姿だから。

    H1  複勝    model_prob >= 0.945 の回収率 > 複勝を全部買う
    H2  拡連複  model_prob >= 0.778 の回収率 > 拡連複を全部買う
    H3  2連複   model_prob >= 0.435 の回収率 > 2連複を全部買う

判定（先に決める）
    主   差（絞った − 全部買う）の95%区間が 0 を含まないこと
         区間はレース単位のブートストラップ
    副   4つの打ち切り窓のうち3つ以上で符号が正であること
    参考 絞ったときの回収率が 100% を超えるか（超えれば勝てる買い方）

対照として「上位20%」規則でも同じ計算を出す（閾値が本番で緩い/きつい
どちらに振れているかを見るため）。**判定は H1〜H3 のみで行う。**

不採用にした賭式（単勝・3連複・3連単）は**測らない**。探索の窓で
通らなかったものを確かめる窓で拾い直すのは、多重比較のやり直しになる。

使い方
------
    python scripts/prob_filter_confirm.py
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
HOLDOUT = ROOT / "data" / "processed" / "wf_holdout.db"

# 本番（docs/js/app.js の BUY_FILTER）と同じ値。ここを変えるときは両方直す。
THRESHOLDS = {"fukusho": 0.945, "kakurenfuku": 0.778, "nirenfuku": 0.435}
TOP_FRACTION = 0.20          # 対照として見る「上位20%」規則


def roi(rows):
    return float(np.mean([r[2] for r in rows])) if rows else float("nan")


def boot_diff(sel, allr, n=3000, seed=0):
    """(絞った − 全部買う) の差の95%区間。レース単位で再抽出する。

    同じレースの行は連動するので行単位では区間が狭く出すぎる。
    絞った側と全部側は**同じレース集合から**引くので、対で抽出する。
    """
    by_race = defaultdict(list)
    for r in allr:
        by_race[r[0]].append(r)
    races = list(by_race.values())
    keys = {(r[0], r[1]) for r in sel}
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        idx = rng.integers(0, len(races), len(races))
        a, s = [], []
        for i in idx:
            for r in races[i]:
                a.append(r)
                if (r[0], r[1]) in keys:
                    s.append(r)
        if s:
            out.append(roi(s) - roi(a))
    if not out:
        return (float("nan"), float("nan"))
    return (float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5)))


def load(db):
    con = sqlite3.connect(db)
    rows = defaultdict(list)     # bet_type -> [(race_key, combo, ret, prob, cutoff)]
    for cu, rd, st, rn, bt, comb, p, ret in con.execute(
            "SELECT cutoff, race_date, stadium, race_no, bet_type, combination,"
            " model_prob, ret FROM picks WHERE ret IS NOT NULL"):
        rows[bt].append(((rd, st, rn), comb, float(ret), float(p), cu))
    con.close()
    return rows


def main():
    if not HOLDOUT.exists():
        print(f"{HOLDOUT} がありません。先に:")
        print("  python scripts/wf_store.py --out data/processed/wf_holdout.db \\")
        print("         --cutoffs 2026-02-01,2026-02-21,2026-03-13,2026-04-02")
        return
    rows = load(HOLDOUT)
    dates = sorted({r[0][0] for v in rows.values() for r in v})
    print("確かめる窓（探索に一度も使っていない）")
    print(f"  {dates[0]} 〜 {dates[-1]}  {len(dates)}日")
    print(f"  レース {len({r[0] for v in rows.values() for r in v}):,}")
    print()
    print("事前登録した仮説: 本番の閾値で絞ると『全部買う』を上回る")
    print(f"{'賭式':12} {'全部買う':>9} {'絞った':>9} {'差':>8} "
          f"{'95%区間':>18} {'本数':>10}  判定")

    verdict = {}
    for bt, th in THRESHOLDS.items():
        allr = rows.get(bt, [])
        if len(allr) < 500:
            print(f"{bt:12} データ不足 ({len(allr)})")
            continue
        sel = [r for r in allr if r[3] >= th]
        if len(sel) < 100:
            print(f"{bt:12} 該当が少なすぎる ({len(sel)}本)")
            continue
        a, s = roi(allr), roi(sel)
        lo, hi = boot_diff(sel, allr)
        ok = lo > 0
        verdict[bt] = ok
        print(f"{bt:12} {a*100:8.1f}% {s*100:8.1f}% {(s-a)*100:+7.1f}pt "
              f"[{lo*100:+6.1f}〜{hi*100:+6.1f}] {len(sel):6,}/{len(allr):<6,} "
              f"{'○ 再現' if ok else '× 再現せず'}")

    print("\n副次: 打ち切り窓ごとの符号（3/4 以上で正なら頑健）")
    for bt, th in THRESHOLDS.items():
        allr = rows.get(bt, [])
        if not allr:
            continue
        marks = []
        for cu in sorted({r[4] for r in allr}):
            sub = [r for r in allr if r[4] == cu]
            sel = [r for r in sub if r[3] >= th]
            if len(sel) < 20:
                marks.append("―")
                continue
            marks.append("+" if roi(sel) > roi(sub) else "−")
        print(f"  {bt:12} {' '.join(marks)}")

    print("\n対照: 『上位20%』規則で同じ計算（判定には使わない）")
    for bt in THRESHOLDS:
        allr = rows.get(bt, [])
        if len(allr) < 500:
            continue
        cut = float(np.percentile([r[3] for r in allr], (1 - TOP_FRACTION) * 100))
        sel = [r for r in allr if r[3] >= cut]
        print(f"  {bt:12} 閾値 {cut:.3f}（本番 {THRESHOLDS[bt]:.3f}）"
              f"  全部 {roi(allr)*100:.1f}% → 絞り {roi(sel)*100:.1f}%")

    print("\n" + "=" * 60)
    if verdict and all(verdict.values()):
        print("すべて再現。確率で絞る買い方は探索の窓の外でも効いている。")
    elif any(verdict.values()):
        print("一部だけ再現。効く賭式と効かない賭式を分けて扱うこと。")
    else:
        print("再現せず。8/31 の結果は探索した窓に固有だった可能性が高い。")
    print("⚠️ 回収率が100%を超えていなければ、良くなっても『損が小さい』止まり。")


if __name__ == "__main__":
    main()
