"""9月の実運用データで、買い方の条件を1回だけ比べる。

⚠️ 事前登録（2026-08-31 22時台・**1件もデータを見る前に書いた**）
==================================================================
9月のレースはまだ1つも走っていない。この時点で仮説・比べ方・判定基準を
固定し、コミットする。**9月が終わるまでこのファイルは変更しない。**

なぜ今書くのか
--------------
2026-08-31 に痛い形で学んだ: 「窓Aで探して窓Bで確かめる」を1つの
データセットの中でやると、どちらの窓も条件選びに関与するので甘く出る。
外部データ（未使用の2〜4月）に出した瞬間、効果が半分に縮んだ。
→ [[project_prob_filter_works]]

**条件をデータより先に決める**のが唯一の逃げ道。だから9月が始まる前に書く。

比べるもの（2連複・賭式ごとに確率が最大の1点。出発点は全部同じ）
--------------------------------------------------------------
    R0  全部買う                                   基準線
    R1  確率>=0.30 かつ EV>=1.2                     ← 現行の本番ルール r5
    R2  確率>=0.387（EVの条件なし）                  ← 案B
    R3  確率>=0.387 かつ EV>=1.2                     ← 両方かける

R2 の 0.387 は**未使用データ(2〜4月)の四分位から決めた値**で、9月の
データからは選んでいない。すべて オッズ 1.5〜50 の範囲内に限る（本番と同条件）。

判定（先に決める）
------------------
    主   R2 − R1 の回収率の差。95%区間が0を含まなければ差ありとする
         区間はレース単位のブートストラップ、同一レース集合から対で抽出
    副   R2 の的中率 > R1 の的中率
    参考 1日あたりの本数（本数が減ると判定に時間がかかる＝実務上の代償）
    参考 R0/R3 も同じ表に出す。ただし**判定に使うのは R2 対 R1 だけ**

⚠️ 検定するのは**この1件のみ**。9月のデータを見てから条件を足したり
賭式を増やしたりしない。それをやると多重比較のやり直しになる。

もう1つ登録しておくこと: 複勝の実地確認
--------------------------------------
2026-08-31 に、複勝が**着順に関係なく全部「外れ」**と判定されていた不具合を
直した（払戻の賭式名の登録漏れ。8/31 の135件で的中0件と記録されていたが
実際は101件当たっていた）。したがって **9月が「正しく判定された複勝」の
最初の1ヶ月**になる。

    H  9月の複勝（記録のみ・全レース）の的中率が 70〜80% に入る
       未使用データの実測は 74.8%（帯ごとに 57.4〜88.9%）

外れたら、直した判定がまだ正しくないか、モデルが変わったかのどちらか。
**これは測定の道具そのものを確かめる検査**なので、勝ち負けとは別に見る。

使い方（9月が終わってから）
--------------------------
    python scripts/sept_trial.py
    python scripts/sept_trial.py --from 2026-09-01 --to 2026-09-30
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"

TRIAL = {"market_blend", "shrink_adj", "top1_value"}
MIN_ODDS, MAX_ODDS = 1.5, 50.0
PROB_CUT = 0.387          # 未使用データ(2〜4月)の2連複 S帯。9月からは選んでいない
EV_CUT = 1.2              # 現行の本番ルール


def load(d_from, d_to):
    """日々の bets JSON から、締切前に確定した買い目だけを読む。

    ⚠️ 確定したものだけを使う。日中に入れ替わる途中の買い目を混ぜると
    「あとから条件を満たしたもの」を拾ってしまう。
    """
    rows = []
    for p in sorted(glob.glob(str(DATA / "bets_2026-*.json"))):
        d = os.path.basename(p)[5:15]
        if not (d_from <= d <= d_to):
            continue
        for b in json.load(open(p, encoding="utf-8")):
            if b.get("rule") in TRIAL:
                continue
            if b.get("bet_type") != "nirenfuku":
                continue
            if not b.get("is_final_pick"):
                continue
            if b.get("is_hit") is None or not b.get("odds"):
                continue
            prob, od = b.get("model_prob") or 0, float(b["odds"])
            rows.append({
                "race": (d, b.get("stadium_name"), b.get("race_no")),
                "date": d, "prob": prob, "odds": od, "ev": prob * od,
                # 100円あたりの払戻 → 倍率
                "ret": (b.get("actual_payout") or 0) / 100.0 if b.get("is_hit") else 0.0,
                "hit": bool(b.get("is_hit")),
            })
    return rows


def stat(rows):
    if not rows:
        return None
    r = np.array([x["ret"] for x in rows])
    return {"n": len(rows), "roi": float(r.mean()),
            "hit": float(np.mean([x["hit"] for x in rows]))}


def boot_diff(a, b, base, n=3000, seed=0):
    """a と b の回収率の差の95%区間。同じレース集合から対で抽出する。"""
    by = defaultdict(list)
    for x in base:
        by[x["race"]].append(x)
    races = list(by.values())
    ka = {x["race"] for x in a}
    kb = {x["race"] for x in b}
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        idx = rng.integers(0, len(races), len(races))
        ra, rb = [], []
        for i in idx:
            for x in races[i]:
                if x["race"] in ka:
                    ra.append(x["ret"])
                if x["race"] in kb:
                    rb.append(x["ret"])
        if ra and rb:
            out.append(float(np.mean(ra)) - float(np.mean(rb)))
    if not out:
        return (float("nan"), float("nan"))
    return (float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5)))


def fukusho_check(d_from, d_to):
    """複勝の的中率が想定の帯に入るか（判定の道具そのものの確認）。"""
    n = hit = 0
    for p in sorted(glob.glob(str(DATA / "bets_2026-*.json"))):
        d = os.path.basename(p)[5:15]
        if not (d_from <= d <= d_to):
            continue
        for b in json.load(open(p, encoding="utf-8")):
            if b.get("bet_type") != "fukusho" or b.get("is_hit") is None:
                continue
            n += 1
            hit += bool(b.get("is_hit"))
    return n, hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d1", default="2026-09-01")
    ap.add_argument("--to", dest="d2", default="2026-09-30")
    a = ap.parse_args()

    rows = load(a.d1, a.d2)
    if len(rows) < 50:
        print(f"{a.d1}〜{a.d2} の確定した2連複は {len(rows)}本。"
              f"まだ判定できない（9月が終わってから回すこと）")
        return
    days = len({r["date"] for r in rows})
    print(f"2026 {a.d1}〜{a.d2}  確定した2連複 {len(rows):,}本 / {days}日\n")

    ok = lambda r: MIN_ODDS <= r["odds"] <= MAX_ODDS          # noqa: E731
    sets = {
        "R0 全部買う": [r for r in rows if ok(r)],
        "R1 現行 確率>=0.30 & EV>=1.2":
            [r for r in rows if ok(r) and r["prob"] >= 0.30 and r["ev"] >= EV_CUT],
        "R2 案B 確率>=0.387（EVなし）":
            [r for r in rows if ok(r) and r["prob"] >= PROB_CUT],
        "R3 確率>=0.387 & EV>=1.2":
            [r for r in rows if ok(r) and r["prob"] >= PROB_CUT and r["ev"] >= EV_CUT],
    }
    print(f"{'ルール':32} {'回収率':>8} {'的中率':>8} {'本数':>7} {'1日':>6}")
    for k, v in sets.items():
        s = stat(v)
        if not s:
            print(f"{k:32} 該当なし")
            continue
        print(f"{k:32} {s['roi']*100:7.1f}% {s['hit']*100:7.1f}% "
              f"{s['n']:7,} {s['n']/days:5.1f}")

    r1, r2 = sets["R1 現行 確率>=0.30 & EV>=1.2"], sets["R2 案B 確率>=0.387（EVなし）"]
    if len(r1) >= 30 and len(r2) >= 30:
        s1, s2 = stat(r1), stat(r2)
        lo, hi = boot_diff(r2, r1, [r for r in rows if ok(r)])
        d = s2["roi"] - s1["roi"]
        print(f"\n主判定  R2 − R1 = {d*100:+.1f}pt  95%区間 [{lo*100:+.1f}〜{hi*100:+.1f}]")
        print("  " + ("差あり（区間が0を含まない）" if lo > 0 or hi < 0
                      else "差なし（区間が0を含む）"))
        print(f"副判定  的中率 R2 {s2['hit']*100:.1f}% vs R1 {s1['hit']*100:.1f}%  "
              f"→ {'R2が上' if s2['hit'] > s1['hit'] else 'R2が上でない'}")
    else:
        print("\n本数不足で判定できない")

    n, hit = fukusho_check(a.d1, a.d2)
    if n:
        print(f"\n複勝の実地確認: {hit}/{n} = {hit/n*100:.1f}%"
              f"  （想定 70〜80%。外れたら判定の道具を疑う）")
    print("\n⚠️ 検定したのは R2 対 R1 の1件のみ。ここから条件を足さないこと。")


if __name__ == "__main__":
    main()
