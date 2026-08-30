"""賭式ごとに「損益分岐を超える区分」を探す。**探す窓と確かめる窓を分ける。**

なぜ手順を固定するか
------------------
2026-08-30 だけで3回、区分探しで「勝てる」と誤認した:

    edge>=2.0 の1点買い    142% → 買える形では 54%
    1号艇の人気薄バイアス   107% → 買える形では消える
    単勝の好成績            モデルの寄与ゼロ（1号艇バイアス）

原因は2つ。**買う時点で手に入らない情報（確定オッズ）で区分を切った**ことと、
**後から良い区分を選んだ**こと。ここでは設計で両方を潰す:

  1. 使うのは `wf_picks.db` だけ。**オッズが入っていない**ので、
     知り得ない情報で切ることが構造的にできない
  2. 仮説を先に決める（機序つき）。データを見てから足さない
  3. 前半の打ち切りだけで探す。後半は見ない
  4. 前半で100%を超えた区分だけを後半で確かめる
  5. 検定した数を必ず報告する（20個試せば1個は偶然通る）
  6. 自明な基準（全部買う）を必ず並べる。上回らなければ意味が無い

使い方:
    python scripts/find_edge.py            # 既定は複勝
    python scripts/find_edge.py nirenfuku
"""
from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

DB = Path("data/processed/wf_picks.db")
MIN_N = 150          # これ未満の区分は区間が広すぎるので判定しない
JP = {"tansho": "単勝", "fukusho": "複勝", "kakurenfuku": "拡連複",
      "nirenfuku": "2連複", "sanrenfuku": "3連複", "sanrentan": "3連単"}


def load(bet_type):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM picks WHERE bet_type=? ORDER BY cutoff, race_date", (bet_type,))]
    cutoffs = sorted({r["cutoff"] for r in rows})
    half = len(cutoffs) // 2
    find = [r for r in rows if r["cutoff"] in set(cutoffs[:half])]
    check = [r for r in rows if r["cutoff"] in set(cutoffs[half:])]
    return find, check, cutoffs


def stat(rows):
    n = len(rows)
    if not n:
        return 0, 0.0, 0.0, None, None
    hit = sum(r["hit"] for r in rows) / n * 100
    roi = sum(r["ret"] for r in rows) / n * 100
    if n < 30:
        return n, hit, roi, None, None
    random.seed(0)
    v = sorted(sum(s) / len(s) * 100 for s in
               ([random.choice(rows)["ret"] for _ in rows] for _ in range(1500)))
    return n, hit, roi, v[37], v[1462]


def line(label, rows, quiet_small=True):
    n, hit, roi, lo, hi = stat(rows)
    if n < MIN_N:
        if not quiet_small:
            print(f"    {label:<22} 母数不足 ({n})")
        return None
    ci = f" [95% {lo:.0f}〜{hi:.0f}]" if lo is not None else ""
    star = "  ★" if lo is not None and lo > 100 else ""
    print(f"    {label:<22} {n:5d}本 的中{hit:5.1f}%  回収{roi:6.1f}%{ci}{star}")
    return roi, lo


def hypotheses(rows):
    """**データを見る前に決めてある。** 機序が説明できるものだけ。"""
    H = {}
    H["H1 モデルの確率"] = [
        (f"{a:.2f}〜{b:.2f}", [r for r in rows if a <= r["model_prob"] < b])
        for a, b in ((0, .5), (.5, .6), (.6, .7), (.7, .8), (.8, 1.01))]
    H["H2 1位と2位の差(自信)"] = [
        (f"{a:.2f}〜{b:.2f}", [r for r in rows if a <= r["gap"] < b])
        for a, b in ((0, .05), (.05, .10), (.10, .20), (.20, 1.01))]
    H["H3 開催場"] = [(s, [r for r in rows if r["stadium"] == s])
                    for s in sorted({r["stadium"] for r in rows})]
    H["H4 グレード"] = [(g, [r for r in rows if r["grade"] == g])
                     for g in sorted({r["grade"] for r in rows})]
    H["H5 ナイター"] = [("ナイター", [r for r in rows if r["is_night"]]),
                     ("デイ", [r for r in rows if not r["is_night"]])]
    H["H6 レース番号"] = [
        (f"{a}〜{b}R", [r for r in rows if a <= r["race_no"] <= b])
        for a, b in ((1, 3), (4, 6), (7, 9), (10, 12))]
    H["H7 1号艇の級別"] = [(c, [r for r in rows if r["b1_class"] == c])
                       for c in ("A1", "A2", "B1", "B2")]
    H["H8 1号艇の全国勝率"] = [
        (f"{a:.1f}〜{b:.1f}", [r for r in rows
                              if r["b1_win_rate"] is not None
                              and a <= r["b1_win_rate"] < b])
        for a, b in ((0, 5.0), (5.0, 6.0), (6.0, 7.0), (7.0, 99))]
    return H


def main():
    bt = sys.argv[1] if len(sys.argv) > 1 else "fukusho"
    if not DB.exists():
        print(f"{DB} がありません。先に scripts/wf_store.py を実行してください")
        return
    find, check, cutoffs = load(bt)
    if not find or not check:
        print(f"{bt} のデータが足りません")
        return
    print(f"【{JP.get(bt, bt)}】損益分岐(100%)を超える区分を探す")
    print(f"探す窓 {cutoffs[:len(cutoffs) // 2]} {len(find)}本")
    print(f"確かめる窓 {cutoffs[len(cutoffs) // 2:]} {len(check)}本")
    print(f"（{MIN_N}本未満の区分は判定しない）")
    print()
    print("=== 自明な基準（これを上回らなければ意味が無い）===")
    line("探す窓・全部買う", find)
    line("確かめる窓・全部買う", check)

    print()
    print("=== 探す窓で仮説を試す ===")
    tested, cands = 0, []
    for name, groups in hypotheses(find).items():
        print(f"  {name}")
        for label, g in groups:
            r = line(label, g)
            if r is None:
                continue
            tested += 1
            if r[0] > 100:
                cands.append((name, label))
    base_find = stat(find)[2]
    print()
    print(f"  判定した区分 {tested}個 / 100%超は {len(cands)}個"
          f"（偶然でも {tested * 0.05:.1f}個ほどは超えうる）")

    if not cands:
        print()
        print(f"探す窓で100%を超える区分は無かった。**ここで打ち切り。**")
        print(f"（全部買うと {base_find:.1f}%。区分を切っても届かない）")
        return

    print()
    print("=== 確かめる窓で同じ区分を見る ===")
    gc = {(n, l): g for n, gs in hypotheses(check).items() for l, g in gs}
    survived = []
    for name, label in cands:
        print(f"  {name} / {label}")
        r = line("確かめる窓", gc.get((name, label), []), quiet_small=False)
        if r and r[0] > 100:
            survived.append((name, label, r))
    print()
    print("=== 両窓とも100%を超えた区分 ===")
    if not survived:
        print("  なし")
        return
    for name, label, (roi, lo) in survived:
        flag = "★下限も100%超" if lo and lo > 100 else "※下限は100%未満（決定的ではない）"
        print(f"  {name} / {label}  回収{roi:.1f}%  {flag}")
    print()
    print("⚠️ ここで終わりではない。採用する前に必ず:")
    print("   1. 買う時点で本当に分かる情報か（確定オッズを使っていないか）")
    print("   2. 自明な基準（全部買う）を上回っているか")
    print("   3. 本数と区間の下限。下限が100%未満なら「まだ分からない」")


if __name__ == "__main__":
    main()
