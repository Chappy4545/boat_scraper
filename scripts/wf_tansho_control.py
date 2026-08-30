"""単勝の「モデルの1点」に本当にモデルの実力があるのかを確かめる。

なぜ要るか
--------
賭式の比較で単勝が両窓とも他を上回った（回収 94.3% / 89.0%、無作為 73.6%）。
だが単勝の的中率は 56% で、**1号艇はもともと5割強勝つ**。
モデルが単に1号艇を選んでいるだけなら、それは市場が完全に織り込んでいる
情報で、実力ではない。同じレースの上で対照を並べる:

    モデルの1点   ランカーのスコア最大
    いつも1号艇   モデルを使わない
    市場の1番人気 確定オッズが最小

「モデル > 1号艇」かつ「モデル > 市場の1番人気」が**両窓**で成り立って
初めて、単勝にモデルの実力があると言える。

使い方:
    python scripts/wf_tansho_control.py
"""
from __future__ import annotations

import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from src.features.builder import build_features, FEATURE_COLS   # noqa: E402
from src.models import plackett_luce as pl                      # noqa: E402
from src.ingestion.database import get_engine, init_db          # noqa: E402
from src.utils.helpers import load_config                       # noqa: E402
from wf_bet_types import train_at, HORIZON                      # noqa: E402

CUTOFFS = ["2026-07-04", "2026-07-24", "2026-08-13"]
KEEP = 0.736     # 単勝の実測取り分（無作為に買ったときの期待回収）


def evaluate(model, d1, d2):
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    if df.empty:
        return []
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce").values.astype(float)
    X = np.where(np.isnan(X), model._medians, X)
    df = df.assign(score=model.predict(X))

    from sqlalchemy import text
    with get_engine().connect() as conn:
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r "
            "ON r.id=o.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND o.is_final=1 AND o.bet_type='tansho' AND o.odds>0"),
            {"d1": d1, "d2": d2}).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no FROM race_results rr JOIN races r "
            "ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order=1"), {"d1": d1, "d2": d2}).fetchall()

    board = defaultdict(dict)
    for rid, cb, o in od:
        board[int(rid)][str(cb)] = float(o)
    winner = {int(rid): str(bn) for rid, bn in res}

    rows = []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        bo, win = board.get(rid, {}), winner.get(rid)
        if len(bo) != 6 or win is None:
            continue
        scores = {int(r.boat_no): float(r.score) for r in grp.itertuples()}
        if len(scores) < 6:
            continue
        exp_s = pl.to_exp_scores(scores, temperature=temp)
        tot = sum(exp_s.values())
        p = {str(b): exp_s[b] / tot for b in sorted(exp_s)}
        model_pick = max(p, key=p.get)
        fav = min(bo, key=bo.get)                 # 市場の1番人気＝オッズ最小
        rows.append({
            "model": (model_pick, bo[win] if model_pick == win else 0.0),
            "boat1": ("1", bo[win] if win == "1" else 0.0),
            "market": (fav, bo[win] if fav == win else 0.0),
            "model_p": p[model_pick], "win": win,
        })
    return rows


def boot(vals, T=2000):
    if len(vals) < 30:
        return None, None
    random.seed(0)
    v = sorted(sum(s) / len(s) * 100 for s in
               ([random.choice(vals) for _ in vals] for _ in range(T)))
    return v[int(.025 * T)], v[int(.975 * T)]


def paired(a, b, T=2000):
    """同じレースでの差（a - b）の95%区間。"""
    d = [x - y for x, y in zip(a, b)]
    if len(d) < 30:
        return None, None
    random.seed(0)
    v = sorted(sum(s) / len(s) * 100 for s in
               ([random.choice(d) for _ in d] for _ in range(T)))
    return v[int(.025 * T)], v[int(.975 * T)]


def report(label, rows):
    n = len(rows)
    print(f"  {label}  {n}レース")
    got = {}
    for who, jp in (("model", "モデルの1点"), ("boat1", "いつも1号艇"),
                    ("market", "市場の1番人気")):
        rets = [r[who][1] for r in rows]
        hits = sum(1 for r in rows if r[who][0] == r["win"]) / n * 100
        roi = sum(rets) / n * 100
        lo, hi = boot(rets)
        got[who] = rets
        print(f"     {jp:<14} 的中{hits:5.2f}%  回収{roi:6.1f}% [95% {lo:.0f}〜{hi:.0f}]"
              f"  無作為{KEEP * 100:.1f}% 差{roi - KEEP * 100:+5.1f}pt")
    for who, jp in (("boat1", "いつも1号艇"), ("market", "市場の1番人気")):
        lo, hi = paired(got["model"], got[who])
        d = (sum(got["model"]) - sum(got[who])) / n * 100
        sig = "有意" if lo is not None and (lo > 0 or hi < 0) else "差なし"
        print(f"     モデル − {jp:<12} {d:+5.1f}pt [95% {lo:+.1f}〜{hi:+.1f}]  {sig}")
    c = Counter(r["model"][0] for r in rows)
    dist = " ".join(f"{k}号艇{c[k] / n * 100:.0f}%" for k in sorted(c))
    print(f"     モデルが選んだ艇: {dist}")
    same = sum(1 for r in rows if r["model"][0] == "1") / n * 100
    agree = sum(1 for r in rows if r["model"][0] == r["market"][0]) / n * 100
    print(f"     1号艇を選んだ率 {same:.0f}%  /  市場の1番人気と一致 {agree:.0f}%")


def main():
    init_db(load_config())
    seed = load_config()["model"].get("random_state", 42)
    print("単勝: モデルの1点 vs いつも1号艇 vs 市場の1番人気（同じレース）")
    print(f"打ち切り {CUTOFFS}")
    print()
    per = {}
    for cu in CUTOFFS:
        d1 = (pd.Timestamp(cu) + pd.Timedelta(days=1)).date().isoformat()
        d2 = (pd.Timestamp(cu) + pd.Timedelta(days=HORIZON)).date().isoformat()
        m, ntr = train_at(cu, seed)
        if m is None:
            continue
        rows = evaluate(m, d1, d2)
        per[cu] = rows
        print(f"打ち切り {cu}（訓練 {ntr}レース）→ 予測 {d1}〜{d2}")
        report("この窓", rows)
        print()
    for name, cus in (("窓A", CUTOFFS[:1]), ("窓B", CUTOFFS[1:])):
        rows = [x for c in cus for x in per.get(c, [])]
        if rows:
            print(f"=== {name} {cus} ===")
            report("合計", rows)
            print()
    print("判定: **両窓とも**「モデル > 1号艇」かつ「モデル > 市場の1番人気」で")
    print("      初めて単勝にモデルの実力があると言える。")


if __name__ == "__main__":
    main()
