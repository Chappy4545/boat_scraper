"""1号艇の単勝はどこで市場より安いのか。**探す窓と確かめる窓を分ける。**

背景
----
2026-08-30 の対照検証で、モデル抜きの観測が1つだけ残った:

    いつも1号艇     的中54.7%   回収 88〜98%
    市場の1番人気   的中54.0〜56.5%   回収 78〜80%
    差 +9.2pt / +16.3pt（両窓で有意）

的中率はほぼ同じなのに配当が高い＝**市場は1号艇を買い足りていない**。
ただし単体では損益分岐(100%)に届かない。どこかに超える区分があるか。

⚠️ 手順を固定する理由
--------------------
同じ日に「後から絞ると必ず良い数字が出る」で1度失敗している
（edge>=2.0 は確定オッズを知っていたから142%に見えただけで、実際は54%）。
区分探しはその罠そのものなので、設計で潰す:

  1. **仮説を先に決める**（機序つき。データを見てから足さない）
  2. 7月だけで探す。8月は見ない
  3. 7月で100%を超えた区分だけを、8月で確かめる
  4. 検定した数を必ず報告する（20個試せば1個は偶然5%水準を通る）

仮説と機序:
  H1 1号艇のオッズ帯     人気薄の1号艇ほど過小評価（フェイバリット・ロングショット）
  H2 1号艇の選手級別     弱く見える選手だと市場が下げすぎる
  H3 1号艇の全国勝率帯   同上を連続量で
  H4 開催場             1コース有利度は場で構造的に違う
  H5 ナイター           賭ける層が違う
  H6 グレード           一般戦と重賞で読み筋が違う
  H7 レース番号          番組編成が違う（1Rは新人・12Rは主力）

使い方:
    python scripts/boat1_edge.py
"""
from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    # cp932 のコンソールだと絵文字で落ちる（2026-08-14 に daily_check が同じ罠を踏み、
    # 静かな失敗を捕まえる点検そのものが静かに失敗した）。置換して落とさない。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

DB = Path(__file__).resolve().parent.parent / "data" / "boatrace.db"
KEEP = 0.736          # 単勝の実測取り分（無作為に買ったときの期待回収）
FIND, CHECK = "2026-07", "2026-08"     # 探す窓 / 確かめる窓
MIN_N = 120           # これ未満の区分は判定しない（区間が広すぎて意味が無い）


def load():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT r.race_date, s.name AS stadium, r.race_no, r.grade, r.is_night,
               o.odds AS odds1,
               (SELECT x.boat_no FROM race_results x
                 WHERE x.race_id=r.id AND x.arrival_order=1) AS winner,
               e.racer_class, e.national_win_rate, e.motor_top2_rate
        FROM odds o
        JOIN races r ON r.id = o.race_id
        JOIN stadiums s ON s.id = r.stadium_id
        LEFT JOIN race_entries e ON e.race_id = r.id AND e.boat_no = 1
        WHERE o.is_final=1 AND o.bet_type='tansho' AND o.combination='1' AND o.odds>0
    """).fetchall()
    out = []
    for r in rows:
        if r["winner"] is None:
            continue
        out.append({
            "month": r["race_date"][:7], "stadium": r["stadium"],
            "race_no": r["race_no"], "grade": r["grade"] or "一般",
            "is_night": bool(r["is_night"]), "odds": float(r["odds1"]),
            "ret": float(r["odds1"]) if int(r["winner"]) == 1 else 0.0,
            "hit": int(int(r["winner"]) == 1),
            "cls": r["racer_class"] or "?",
            "nwr": r["national_win_rate"], "motor": r["motor_top2_rate"],
        })
    return out


def stat(rows):
    n = len(rows)
    if not n:
        return 0, 0.0, 0.0, None, None
    hit = sum(x["hit"] for x in rows) / n * 100
    roi = sum(x["ret"] for x in rows) / n * 100
    if n < 30:
        return n, hit, roi, None, None
    random.seed(0)
    v = sorted(sum(s) / len(s) * 100 for s in
               ([random.choice(rows)["ret"] for _ in rows] for _ in range(2000)))
    return n, hit, roi, v[50], v[1949]


def line(label, rows, mark_over100=True):
    n, hit, roi, lo, hi = stat(rows)
    if n < MIN_N:
        return None
    ci = f" [95% {lo:.0f}〜{hi:.0f}]" if lo is not None else ""
    star = "  ★" if (mark_over100 and lo is not None and lo > 100) else ""
    print(f"    {label:<22} {n:4d}本 的中{hit:5.1f}%  回収{roi:6.1f}%{ci}{star}")
    return roi, lo


def hypotheses(rows):
    """仮説ごとに区分を作る。**データを見る前にここを決めてある。**"""
    H = {}
    H["H1 1号艇のオッズ帯"] = [
        (f"{a}〜{b}倍", [x for x in rows if a <= x["odds"] < b])
        for a, b in ((1.0, 1.3), (1.3, 1.6), (1.6, 2.0), (2.0, 3.0), (3.0, 99))]
    H["H2 1号艇の級別"] = [
        (c, [x for x in rows if x["cls"] == c]) for c in ("A1", "A2", "B1", "B2")]
    H["H3 1号艇の全国勝率"] = [
        (f"{a:.1f}〜{b:.1f}", [x for x in rows
                              if x["nwr"] is not None and a <= x["nwr"] < b])
        for a, b in ((0, 5.0), (5.0, 6.0), (6.0, 7.0), (7.0, 99))]
    H["H4 開催場"] = [(s, [x for x in rows if x["stadium"] == s])
                    for s in sorted({x["stadium"] for x in rows})]
    H["H5 ナイター"] = [("ナイター", [x for x in rows if x["is_night"]]),
                     ("デイ", [x for x in rows if not x["is_night"]])]
    H["H6 グレード"] = [(g, [x for x in rows if x["grade"] == g])
                     for g in sorted({x["grade"] for x in rows})]
    H["H7 レース番号"] = [
        (f"{a}〜{b}R", [x for x in rows if a <= x["race_no"] <= b])
        for a, b in ((1, 3), (4, 6), (7, 9), (10, 12))]
    return H


def main():
    rows = load()
    find = [x for x in rows if x["month"] == FIND]
    check = [x for x in rows if x["month"] == CHECK]
    print("1号艇の単勝はどこで市場より安いのか")
    print(f"探す窓 {FIND} {len(find)}レース / 確かめる窓 {CHECK} {len(check)}レース")
    print(f"無作為に買ったときの期待回収 {KEEP * 100:.1f}%。損益分岐は 100%")
    print(f"（{MIN_N}本未満の区分は区間が広すぎるので判定しない）")
    print()
    print("=== 全体 ===")
    line("探す窓(7月)", find)
    line("確かめる窓(8月)", check)

    print()
    print(f"=== 探す窓({FIND})で仮説を試す ===")
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
    print()
    print(f"  判定した区分 {tested}個。うち100%を超えたのは {len(cands)}個")
    print(f"  ⚠️ 偶然でも {tested * 0.05:.1f}個ほどは超えうる。だから次で確かめる。")

    if not cands:
        print()
        print("探す窓で100%を超える区分は無かった。**ここで打ち切り。**")
        return

    print()
    print(f"=== 確かめる窓({CHECK})で同じ区分を見る ===")
    groups_c = {(n, l): g for n, gs in hypotheses(check).items() for l, g in gs}
    survived = []
    for name, label in cands:
        g = groups_c.get((name, label), [])
        print(f"  {name} / {label}")
        r = line("確かめる窓", g)
        if r and r[0] > 100:
            survived.append((name, label))
    print()
    print("=== 両窓とも100%を超えた区分 ===")
    print(f"  {survived if survived else 'なし'}")
    if survived:
        print("  ※ これでも「見つけた」ではない。本数と区間の下限を必ず見ること。")


if __name__ == "__main__":
    main()
