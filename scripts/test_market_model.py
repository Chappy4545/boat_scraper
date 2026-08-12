"""市場の誤りを直接学習する — 「レース結果」ではなく「市場のどこが間違っているか」を当てる。

これまでのモデルはレース結果を予測し、その確率とオッズを比べていた。
だが市場（オッズ）はモデルより正確で（対数損失 0.194 vs 0.195）、
勝てるのは市場が系統的に外している場面だけ。

ならば最初から「市場の見立てと実際のズレ」を学習した方が素直ではないか。

3つを比べる:
  A. 現行     モデル確率だけで判断
  B. 市場込み 特徴量に市場確率を加えて結果を予測
  C. 誤り学習 市場確率からの「ズレ」を予測（残差学習）

検証は必ず学習期間より後。オッズは is_live=1（買う時点で見えた値）のみ。

使い方: python scripts/test_market_model.py <train_from> <train_to> <test_from> <test_to>
"""
from __future__ import annotations

import logging
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features.builder import build_features, FEATURE_COLS
from src.models import plackett_luce as pl
from src.models.trainer import load_ranker
from src.ingestion.database import get_engine

BT = "nirenfuku"


def collect(df, engine, d1, d2, ranker):
    """組合せ単位のデータを作る: モデル確率・市場確率・オッズ・的中・払戻"""
    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": [BT]}
    with engine.connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.combination,p.payout FROM payouts p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r ON r.id=o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_live=1 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()

    payout = {(int(r[0]), str(r[1])): float(r[2]) for r in pay}
    won = defaultdict(set)
    for r in pay:
        won[int(r[0])].add(str(r[1]))
    odds = defaultdict(dict)
    for rid, cb, o in od:
        odds[int(rid)][str(cb)] = float(o)

    sub = df[(df["race_date"].astype(str) >= d1) & (df["race_date"].astype(str) <= d2)]
    X = sub[FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    sub = sub.assign(_s=ranker.predict(X.fillna(X.median()).values))

    rows = []
    for race_id, g in sub.groupby("race_id", sort=False):
        rid = int(race_id)
        if rid not in odds or rid not in won or len(odds[rid]) < 15:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_s"])}
        if len(scores) < 6:
            continue
        inv = {cb: 1.0 / o for cb, o in odds[rid].items() if o > 0}
        tot = sum(inv.values())
        if tot <= 0:
            continue
        # レース単位の特徴（6艇の平均などは使わず、組合せ単位で作る）
        for c in pl.all_bet_probs(scores, temperature=1.0).get(BT, []):
            cb = c["combination"]
            o = odds[rid].get(cb)
            if o is None:
                continue
            a, b = (int(x) for x in cb.split("-"))
            rows.append({
                "race_id": rid,
                "pm": float(c["model_prob"]),
                "pk": inv[cb] / tot,
                "odds": o,
                "y": 1 if cb in won[rid] else 0,
                "payout": payout.get((rid, cb), 0.0),
                # 組合せの素性（どの艇の組か。1号艇絡みかどうかは効きうる）
                "has1": 1 if 1 in (a, b) else 0,
                "sum_no": a + b,
                "score_a": scores.get(a, 0.0),
                "score_b": scores.get(b, 0.0),
            })
    return pd.DataFrame(rows)


def sim(sel, label):
    if len(sel) < 20:
        print(f"    {label:<26}{len(sel):>6}  （少なすぎ）"); return
    stake = len(sel) * 100
    ret = sel["payout"].sum()
    roi = ret / stake * 100
    mark = "  ←黒字" if roi > 100 else ""
    print(f"    {label:<26}{len(sel):>6,}{sel['y'].mean()*100:>7.1f}%{roi:>8.1f}%{mark}")


def main():
    tr_from, tr_to, te_from, te_to = sys.argv[1:5]
    engine = get_engine()
    ranker = load_ranker()

    df = build_features(None, te_to, include_target=True).dropna(subset=["target_win"])
    print(f"学習 {tr_from}〜{tr_to} / 検証 {te_from}〜{te_to}")
    tr = collect(df, engine, tr_from, tr_to, ranker)
    te = collect(df, engine, te_from, te_to, ranker)
    print(f"  学習 {len(tr):,} 件 / 検証 {len(te):,} 件\n")
    if tr.empty or te.empty:
        print("データ不足"); return

    import lightgbm as lgb
    FEAT_B = ["pm", "pk", "odds", "has1", "sum_no", "score_a", "score_b"]

    # B: 市場込みで「当たるか」を直接予測
    mb = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=31,
                            min_child_samples=50, random_state=42, verbose=-1)
    mb.fit(tr[FEAT_B], tr["y"])
    te["pb"] = mb.predict_proba(te[FEAT_B])[:, 1]

    # C: 市場確率からの「ズレ」を学習（残差）
    mc = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.05, num_leaves=31,
                           min_child_samples=50, random_state=42, verbose=-1)
    mc.fit(tr[FEAT_B], tr["y"] - tr["pk"])
    te["pc"] = (te["pk"] + mc.predict(te[FEAT_B])).clip(0.001, 0.999)

    def ll(p):
        p = np.clip(p, 1e-9, 1 - 1e-9)
        return float(np.mean(-(te["y"] * np.log(p) + (1 - te["y"]) * np.log(1 - p))))

    print("  【確率の精度】対数損失（小さいほど良い）")
    print(f"    A 現行モデル      {ll(te['pm']):.5f}")
    print(f"    -  市場そのもの   {ll(te['pk']):.5f}")
    print(f"    B 市場込みで予測  {ll(te['pb']):.5f}")
    print(f"    C 市場の誤りを学習 {ll(te['pc']):.5f}")

    print("\n  【回収率】確率30%以上 かつ EV1.2以上（オッズ1.5〜50倍）")
    print(f"    {'手法':<26}{'本数':>6}{'的中率':>8}{'回収率':>9}")
    base = te[(te["odds"] >= 1.5) & (te["odds"] <= 50)]
    for col, label in [("pm", "A 現行モデル"), ("pb", "B 市場込みで予測"),
                       ("pc", "C 市場の誤りを学習")]:
        sim(base[(base[col] >= 0.30) & (base[col] * base["odds"] >= 1.2)], label)

    print("\n  【参考】閾値を変えた場合（C の手法）")
    for th, ev in [(0.20, 1.2), (0.25, 1.2), (0.30, 1.1), (0.35, 1.2)]:
        sim(base[(base["pc"] >= th) & (base["pc"] * base["odds"] >= ev)],
            f"C p>={th:.2f} & EV>={ev}")


if __name__ == "__main__":
    main()
