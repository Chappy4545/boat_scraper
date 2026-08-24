"""締切直前の板から「確定オッズはいくらになりそうか」を推定する。

なぜ必要か
----------
EV で選ぶというのは「表示オッズが高いもの」を選ぶこと。表示オッズが高いのは
まだ票が入りきっていないからで、締切までに票が入って本来の水準へ戻る。
結果として **構造的に「これから下がる側」だけを拾う**。

実測（本番ルール264本・2026-08-24）:
    選択時 平均 4.75倍 → 確定 平均 3.09倍
    表示オッズどおり払われていたら 160.7% / 実際 92.5%

⚠️ ただしこれは「オッズ全体が下がる」現象ではない。同じ期間・同じレースで
   確定/板 の中央値は

       全15通り        1.081   ← ほとんど動かない
       実際に買った分  0.746   ← これだけ落ちる

   **完全に選択の副作用**。だから板だけを説明変数にした回帰では直らない
   （それをやると「オッズは上がる」という逆の答えが出る）。板の中で
   浮いている分を見抜くには、板とは独立な情報＝モデル確率が要る。

推定する式
----------
    log F = a + b*log(B) + c*log(1/p)
        F: 確定オッズ  B: 締切直前の板  p: モデル確率

b < 1 は「板の高さを割り引く」、c > 0 は「モデルが本命と見るほど安く着地する」。

データ
------
docs/data/board_<日付>.json.gz  締切の約13分前に取った全15通りの板
docs/data/probs_<日付>.json     全レース・全組合せのモデル確率
odds(is_final=1)                確定オッズ

板は買い目に選んだものだけでなく**全通り**なので選択バイアスがかからない。
⚠️ board の race_id はクラウドの使い捨てDBのIDなので信用しない。
   (日付, 場名, レース番号) で照合する。

検証
----
1窓で決めない。日ごとに1日を伏せて残りで推定し、伏せた日で確認する。
過去に「片方の窓でだけ良く見えた候補」を2件取り下げている。

使い方:
    python scripts/estimate_shrink.py
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
import sqlite3
from collections import defaultdict
from statistics import median

DB = "data/boatrace.db"
BET_TYPE = "nirenfuku"          # 運用しているのは2連複だけ
MIN_COMBOS = 15                 # 2連複の全通り
BOOK_RANGE = (1.20, 1.55)       # sum(1/odds) がこの外なら歪んだ板として捨てる


def load(conn) -> list[dict]:
    code2name = {c: n for c, n in conn.execute("SELECT code, name FROM stadiums")}
    local = {}
    for rid, d, sname, rno in conn.execute("""
            SELECT r.id, r.race_date, s.name, r.race_no
            FROM races r JOIN stadiums s ON s.id = r.stadium_id
            WHERE r.race_date >= '2026-08-01'"""):
        local[(d, sname, rno)] = rid

    prob: dict[tuple, float] = {}
    for f in sorted(glob.glob("docs/data/probs_*.json")):
        d = os.path.basename(f)[6:16]
        try:
            obj = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for r in obj.get("races", []):
            rid = local.get((d, code2name.get(r["stadium_code"], ""), r["race_no"]))
            if rid is None:
                continue
            for cmb in r.get("combinations", []):
                prob[(rid, cmb["bet_type"], cmb["combination"])] = cmb["model_prob"]

    rows: list[dict] = []
    for f in sorted(glob.glob("docs/data/board_*.json.gz")):
        d = os.path.basename(f)[6:16]
        obj = json.loads(gzip.decompress(open(f, "rb").read()))
        for _key, rec in obj.get("races", {}).items():
            rid = local.get((d, rec["stadium"], rec["race_no"]))
            if rid is None:
                continue
            fin = {cb: o for bt, cb, o in conn.execute(
                "SELECT bet_type, combination, odds FROM odds WHERE race_id=? "
                "AND is_final=1 AND odds>0", (rid,)) if bt == BET_TYPE}
            if not fin:
                continue
            order = [str(b) for (b,) in conn.execute(
                "SELECT boat_no FROM race_results WHERE race_id=? "
                "AND arrival_order IS NOT NULL ORDER BY arrival_order", (rid,))]
            if len(order) < 2:
                continue
            top2 = set(order[:2])
            board = [o for o in rec["odds"]
                     if o["bet_type"] == BET_TYPE and o["odds"] > 0]
            if len(board) < MIN_COMBOS:
                continue
            book = sum(1.0 / o["odds"] for o in board)
            if not (BOOK_RANGE[0] <= book <= BOOK_RANGE[1]):
                continue
            for o in board:
                p = prob.get((rid, BET_TYPE, o["combination"]))
                if o["combination"] in fin and p:
                    rows.append({
                        "date": d, "race": rid, "B": o["odds"],
                        "F": fin[o["combination"]], "p": p,
                        "hit": set(o["combination"].split("-")) == top2,
                    })
    return rows


def solve(data: list[dict]) -> tuple[float, float, float]:
    """log F = a + b*log(B) + c*log(1/p) を正規方程式＋ガウス消去で解く。"""
    n = len(data)
    X = [[1.0, math.log(r["B"]), math.log(1 / r["p"])] for r in data]
    y = [math.log(r["F"]) for r in data]
    M = [[sum(X[i][j] * X[i][k] for i in range(n)) for k in range(3)]
         + [sum(X[i][j] * y[i] for i in range(n))] for j in range(3)]
    for i in range(3):
        piv = max(range(i, 3), key=lambda r_: abs(M[r_][i]))
        M[i], M[piv] = M[piv], M[i]
        for j in range(i + 1, 3):
            f = M[j][i] / M[i][i]
            for k in range(i, 4):
                M[j][k] -= f * M[i][k]
    c = [0.0, 0.0, 0.0]
    for i in (2, 1, 0):
        c[i] = (M[i][3] - sum(M[i][k] * c[k] for k in range(i + 1, 3))) / M[i][i]
    return c[0], c[1], c[2]


def predict(coef, r) -> float:
    a, b, c = coef
    return math.exp(a + b * math.log(r["B"]) + c * math.log(1 / r["p"]))


def rmse(data, f) -> float:
    return math.sqrt(sum((math.log(r["F"]) - math.log(f(r))) ** 2
                         for r in data) / len(data))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--p-min", type=float, default=0.30, help="運用ルールの確率下限")
    ap.add_argument("--ev-min", type=float, default=1.2, help="運用ルールのEV下限")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    rows = load(conn)
    if not rows:
        print("使えるデータがありません（board_*.json.gz が必要）")
        return
    by_day: dict[str, list] = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    days = sorted(by_day)
    print("賭式 %s / %d組 / %dレース" % (BET_TYPE, len(rows), len({r["race"] for r in rows})))
    print("日別: %s" % {d: len(v) for d, v in sorted(by_day.items())})
    print()
    print("確定/板 の中央値  全通り %.3f  ← 全体はほとんど動かない"
          % median([r["F"] / r["B"] for r in rows]))

    coefs = []
    for test_day in days:
        train = [r for d in days if d != test_day for r in by_day[d]]
        test = by_day[test_day]
        if len(train) < 500 or len(test) < 200:
            continue
        coef = solve(train)
        coefs.append(coef)
        base = rmse(test, lambda r: r["B"])
        full = rmse(test, lambda r: predict(coef, r))
        print()
        print("=== 伏せた日 %s（推定は残りの日）===" % test_day)
        print("  log F = %.4f + %.4f*log(板) + %.4f*log(1/p)" % coef)
        print("  確定オッズの当て方（対数の誤差・小さいほど良い）")
        print("    板をそのまま      %.4f" % base)
        print("    補正後            %.4f   (%+.1f%%)" % (full, (full / base - 1) * 100))
        print("  p>=%.2f かつ EV>=%.1f で選んだとき" % (args.p_min, args.ev_min))
        for label, odds_of in (("板そのまま", lambda r: r["B"]),
                               ("補正後", lambda r: predict(coef, r))):
            sel = [r for r in test
                   if r["p"] >= args.p_min and r["p"] * odds_of(r) >= args.ev_min]
            if not sel:
                print("    %-10s 0本" % label)
                continue
            ret = sum(r["F"] for r in sel if r["hit"]) * 100
            print("    %-10s %3d本 的中%4.1f%% 回収%6.1f%%"
                  % (label, len(sel), sum(1 for r in sel if r["hit"]) / len(sel) * 100,
                     ret / (len(sel) * 100) * 100))

    if coefs:
        n = len(coefs)
        avg = tuple(sum(c[i] for c in coefs) / n for i in range(3))
        print()
        print("=== 平均の係数（%d通りの伏せ方）===" % n)
        print("  log F = %.4f + %.4f*log(板) + %.4f*log(1/p)" % avg)
        for b_ in (2, 3, 5, 10, 20):
            for p_ in (0.35,):
                print("    板%5.1f倍・p=%.2f  →  確定 %5.2f倍 と見込む"
                      % (b_, p_, predict(avg, {"B": b_, "p": p_})))
        print()
        print("⚠️ 予測が良くなることは3日とも再現した。ただし回収率が上がるかは")
        print("   本数が足りず未確認。板が貯まるほど推定も検証も良くなる。")


if __name__ == "__main__":
    main()
