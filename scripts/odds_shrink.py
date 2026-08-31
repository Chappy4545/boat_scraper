"""買った時のオッズが締切までにどれだけ縮むかを、層ごとに測る。

なぜ要るか
----------
`daily_check` が毎晩「見込みオッズの精度 NG（推定/確定 中央値 1.36）」を
出している。買う時に見ているオッズが確定オッズより36%高い＝**EVを36%
過大評価している**＝本来買わない買い目を買っていることになる。

ただしこの数字は候補ルール(top1_value, EV>=2.0)だけを見たもの。
本番ルール(r5)や全組合せでどうなのかは測っていない。

⚠️ 縮みは「推定が下手」とは限らない
-----------------------------------
[[project_odds_board_vs_final]] の実測では、全通りだと 1.079 なのに
買った分だけ見ると 0.746 だった。**EVで選ぶ＝オッズが上振れしている
組合せを選ぶ**ので、選んだ分だけ平均に回帰して縮むのは当たり前
（optimizer's curse）。補正を入れると回収率が逆に悪化した前例もある。

なので測る順番は:
    1. 全組合せの縮み        （市場全体の性質）
    2. 買った分の縮み        （選択の副作用込み）
    3. その差               ＝選択が持ち込んでいる分
    4. EV帯・賭式ごとの内訳   （どこで効くか）

使い方
------
    python scripts/odds_shrink.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIVE_SINCE = "2026-08-11"       # 実運用の開始日


def med(xs):
    return float(np.median(xs)) if len(xs) else float("nan")


def line(label, xs, extra=""):
    if len(xs) < 10:
        print(f"  {label:30} {'—':>7}  (本数不足 {len(xs)})")
        return
    a = np.array(xs)
    print(f"  {label:30} {med(a):7.3f}  平均{a.mean():6.3f}  "
          f"25%{np.percentile(a, 25):6.3f}  75%{np.percentile(a, 75):6.3f}  "
          f"{len(a):6,}本 {extra}")


def main():
    from sqlalchemy import text
    from src.ingestion.database import init_db, get_engine
    from src.utils.helpers import load_config

    cfg = load_config()
    init_db(cfg)

    # ── 1. 全組合せ（市場全体の性質）──────────────────
    # 朝の板(is_final=0) と 確定(is_final=1) を同じ組合せで突き合わせる。
    sql_all = """
        SELECT b0.bet_type, b0.odds, b1.odds
          FROM odds b0
          JOIN odds b1 ON b1.race_id = b0.race_id
                      AND b1.bet_type = b0.bet_type
                      AND b1.combination = b0.combination
                      AND b1.is_final = 1
          JOIN races r ON r.id = b0.race_id
         WHERE b0.is_final = 0 AND b0.odds > 0 AND b1.odds > 0
           AND r.race_date >= :s
    """
    by_type_all = defaultdict(list)
    with get_engine().connect() as conn:
        for bt, o0, o1 in conn.execute(text(sql_all), {"s": LIVE_SINCE}).all():
            by_type_all[bt].append(o0 / o1)

    print(f"=== 1. 全組合せ（朝の板 ÷ 確定オッズ）{LIVE_SINCE} 以降 ===")
    print("  1.0 より大きい = 朝の方が高い（締切までに縮む）")
    allv = [v for xs in by_type_all.values() for v in xs]
    line("全賭式", allv)
    for bt in sorted(by_type_all):
        line(f"  {bt}", by_type_all[bt])

    # ── 2〜4. 買い目（買った時のオッズ ÷ 確定）────────────
    # 見込みオッズは列が無いので expected_value / model_prob で戻す。
    sql_bet = """
        SELECT b.pass_reason, b.bet_type, b.expected_value, b.model_prob,
               o.odds, b.recommended_amount, b.is_hit
          FROM bets b
          JOIN races r ON r.id = b.race_id
          JOIN odds o ON o.race_id = b.race_id AND o.bet_type = b.bet_type
                     AND o.combination = b.combination AND o.is_final = 1
         WHERE b.model_prob > 0 AND o.odds > 0 AND r.race_date >= :s
           AND b.expected_value IS NOT NULL
           AND date(b.created_at) <= r.race_date
    """
    buy, cand, rec, by_ev, by_type_buy = [], [], [], defaultdict(list), defaultdict(list)
    with get_engine().connect() as conn:
        for reason, bt, ev, mp, fin, amt, _hit in conn.execute(
                text(sql_bet), {"s": LIVE_SINCE}).all():
            used = ev / mp
            ratio = used / fin
            if (amt or 0) > 0:
                buy.append(ratio)
                by_type_buy[bt].append(ratio)
                band = ("EV>=3.0" if ev >= 3.0 else "EV 2.0-3.0" if ev >= 2.0
                        else "EV 1.5-2.0" if ev >= 1.5 else "EV 1.2-1.5")
                by_ev[band].append(ratio)
            elif reason and "候補" in str(reason):
                cand.append(ratio)
            elif reason and "記録" in str(reason):
                rec.append(ratio)

    print(f"\n=== 2. 買い目（買った時のオッズ ÷ 確定オッズ）===")
    line("買った買い目(金額>0)", buy)
    line("候補ルール", cand)
    line("記録のみ", rec)

    print(f"\n=== 3. 選択が持ち込んでいる分 ===")
    if len(allv) >= 10 and len(buy) >= 10:
        print(f"  全組合せ {med(allv):.3f} → 買った分 {med(buy):.3f}  "
              f"差 {med(buy) - med(allv):+.3f}")
        print("  ※ 差が大きいほど「EVで選んだせいで縮む」分が大きい"
              "（optimizer's curse）")

    print(f"\n=== 4. EV帯ごと（買った買い目）===")
    for band in ("EV 1.2-1.5", "EV 1.5-2.0", "EV 2.0-3.0", "EV>=3.0"):
        line(band, by_ev.get(band, []))

    print(f"\n=== 5. 賭式ごと（買った買い目）===")
    for bt in sorted(by_type_buy):
        line(bt, by_type_buy[bt])

    # ── 6. 2つの窓で安定しているか ──────────────────
    # 補正表として使えるのは「前半で作った表が後半でも当たる」場合だけ。
    # 片方の窓でしか出ない関係は、いつもの雑音。
    print(f"\n=== 6. 前半と後半で同じ関係が出るか ===")
    sql_days = """
        SELECT DISTINCT r.race_date FROM bets b JOIN races r ON r.id = b.race_id
         WHERE b.recommended_amount > 0 AND r.race_date >= :s ORDER BY 1
    """
    with get_engine().connect() as conn:
        days = [str(x[0]) for x in conn.execute(text(sql_days),
                                                {"s": LIVE_SINCE}).all()]
    if len(days) < 6:
        print("  日数が足りない")
        return
    half = len(days) // 2
    win = {d: ("A" if i < half else "B") for i, d in enumerate(days)}

    sql_w = sql_bet.replace("SELECT b.pass_reason",
                            "SELECT r.race_date, b.pass_reason")
    bands = defaultdict(lambda: defaultdict(list))
    with get_engine().connect() as conn:
        for rd, _reason, _bt, ev, mp, fin, amt, _hit in conn.execute(
                text(sql_w), {"s": LIVE_SINCE}).all():
            if (amt or 0) <= 0:
                continue
            w = win.get(str(rd))
            if not w:
                continue
            band = ("EV>=3.0" if ev >= 3.0 else "EV 2.0-3.0" if ev >= 2.0
                    else "EV 1.5-2.0" if ev >= 1.5 else "EV 1.2-1.5")
            bands[band][w].append((ev / mp) / fin)

    print(f"  窓A {days[0]}〜{days[half-1]} / 窓B {days[half]}〜{days[-1]}")
    print(f"  {'EV帯':14} {'窓A':>18} {'窓B':>18}   判定")
    for band in ("EV 1.2-1.5", "EV 1.5-2.0", "EV 2.0-3.0", "EV>=3.0"):
        a, b = bands[band]["A"], bands[band]["B"]
        if len(a) < 10 or len(b) < 10:
            print(f"  {band:14} {'本数不足':>18} "
                  f"{f'(A{len(a)} B{len(b)})':>18}")
            continue
        ma, mb = med(a), med(b)
        ok = abs(ma - mb) / max(ma, mb) < 0.25
        print(f"  {band:14} {ma:9.3f}({len(a):4}本) {mb:9.3f}({len(b):4}本)   "
              f"{'一致' if ok else '⚠️ 食い違い'}")


if __name__ == "__main__":
    main()
