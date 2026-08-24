"""Plackett-Luce 変換そのものが、どれだけ情報を捨てているかを測る。

問題意識:
  「艇単位では市場に勝ち、組単位では負ける」と出た。だが単勝市場と
  2連複市場は別物で、単勝の方が売上が薄い＝非効率なぶん勝ちやすいだけ、
  という可能性がある。モデルの話とPLの話が混ざっている。

切り分け:
  **市場自身の単勝確率**を同じPL変換にかけて2連複確率を作り、
  市場が実際につけている2連複確率と比べる。
  どちらも同じ市場・同じレースなので、差はPL変換だけに由来する。

    PL(市場の単勝) が 市場の2連複 より悪い → PL変換が情報を捨てている
    ほぼ同じ                              → PLは無実。モデル側の話

使い方:
    python scripts/pl_cost.py 2026-08-01 2026-08-23
"""
from __future__ import annotations

import math
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import plackett_luce as pl        # noqa: E402
from src.ingestion.database import get_engine     # noqa: E402


def clip(p: float) -> float:
    return min(max(p, 1e-9), 1 - 1e-9)


def logloss(rows) -> float:
    return sum(-(math.log(clip(p)) if y else math.log(1 - clip(p)))
               for p, y in rows) / len(rows)


def main() -> None:
    d1, d2 = sys.argv[1], sys.argv[2]
    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": ["tansho", "nirenfuku"]}
    with get_engine().connect() as conn:
        od = conn.execute(text(
            "SELECT o.race_id,o.bet_type,o.combination,o.odds FROM odds o "
            "JOIN races r ON r.id=o.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND o.is_final=1 AND o.odds>0 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no,rr.arrival_order FROM race_results rr "
            "JOIN races r ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order IS NOT NULL"
        ), prm).fetchall()

    odds = defaultdict(lambda: defaultdict(dict))
    for rid, bt, cb, o in od:
        odds[int(rid)][bt][str(cb)] = float(o)
    order = defaultdict(dict)
    for rid, bn, ao in res:
        order[int(rid)][int(ao)] = str(bn)

    groups = []          # レースごとに [(pl由来, 市場の2連複, 的中)]
    for rid, o in odds.items():
        t, nf = o.get("tansho", {}), o.get("nirenfuku", {})
        fin = order.get(rid, {})
        if len(t) != 6 or len(nf) != 15 or 1 not in fin or 2 not in fin:
            continue
        top2 = {fin[1], fin[2]}
        # 市場の単勝確率（控除を除いて正規化）
        tt = sum(1.0 / v for v in t.values())
        q_win = {int(k): (1.0 / v) / tt for k, v in t.items()}
        # それを PL に通して2連複確率へ
        exp_s = pl.to_exp_scores({k: math.log(max(v, 1e-9)) for k, v in q_win.items()},
                                 temperature=1.0)
        # 市場の2連複確率
        nt = sum(1.0 / v for v in nf.values())
        rows = []
        for cb, v in nf.items():
            a, b = (int(x) for x in cb.split("-"))
            rows.append((pl.joint_prob_nirenfuku(exp_s, a, b),
                         (1.0 / v) / nt,
                         set(cb.split("-")) == top2))
        if len(rows) == 15:
            groups.append(rows)

    if len(groups) < 50:
        raise SystemExit("データ不足: %d レース" % len(groups))

    flat = [x for g in groups for x in g]
    ll_pl = logloss([(p, y) for p, _q, y in flat])
    ll_mk = logloss([(q, y) for _p, q, y in flat])

    def gap(sample):
        f = [x for g in sample for x in g]
        n = len(f)
        d = mk = 0.0
        for p, q, y in f:
            lp = -(math.log(clip(p)) if y else math.log(1 - clip(p)))
            lq = -(math.log(clip(q)) if y else math.log(1 - clip(q)))
            d += lq - lp
            mk += lq
        return d / mk * 100

    random.seed(0)
    boot = sorted(gap([random.choice(groups) for _ in groups]) for _ in range(2000))

    print("同一市場・同一レースで、PL変換のコストだけを測る")
    print("（%s 〜 %s / %d レース / %d 組合せ）" % (d1, d2, len(groups), len(flat)))
    print()
    print("  市場の2連複オッズそのもの      対数損失 %.5f" % ll_mk)
    print("  市場の単勝をPLで2連複にした値  対数損失 %.5f" % ll_pl)
    print()
    print("  PL変換の損失 %+.2f%%   95%%区間 [%+.2f%%, %+.2f%%]"
          % (gap(groups), boot[50], boot[1949]))
    print("  （負なら PL を通した方が悪い＝変換で情報を捨てている）")
    print()
    if boot[1949] < 0:
        print("  → PL変換だけで %.1f%% 失っている。モデルの良し悪しとは無関係。"
              % abs(gap(groups)))
    elif boot[50] > 0:
        print("  → PL を通した方が良い。想定外なので条件を疑うこと。")
    else:
        print("  → 差なし。PLは無実で、負けはモデル側の問題。")


if __name__ == "__main__":
    main()
