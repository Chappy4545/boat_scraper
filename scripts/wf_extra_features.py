"""直前情報（展示・気象）を足すと**回収率**が上がるのかを、未見データで測る。

なぜもう一度測るのか
------------------
2026-08-13 に一度試して棄却されている（builder.py の EXTRA_FEATURE_COLS に記録）:

    07-11〜08-12  47項目 vs 34項目  +0.52% ±0.42  有意
    05-01〜07-10  47項目 vs 34項目  -0.38% ±0.57  誤差の範囲
    → 片方の窓でしか出ないので不採用

**ただしあれは対数損失（精度）の比較**で、回収率は見ていない。
このプロジェクトには「較正の改善≠収益の改善」という実測がある
（PL補正は対数損失を改善したが回収率は 113%→87% に悪化した）。
逆もありうる: 精度が横ばいでも、買う1点の選び方が変われば回収率は動く。

目的は「勝てるモデル」なので、**回収率で測り直す**。

設計
----
- 同じ `build_features` の出力から**列だけ変えて**2モデルを訓練する
  （環境変数で切り替えると1プロセスで両方を扱えない）
- 打ち切り日までで訓練 → 翌日から20日を予測。1日も重ねない
- 同じレース・同じ組合せの上で対にして比べる（集計値の引き算はしない）
- 独立2窓（前半2打ち切り / 後半2打ち切り）。片窓の偽陽性を弾くため

⚠️ 直前情報の被覆が 90% 以上ある区間は **2026-05-30〜08-11 の74日**だけ
（05-21〜05-29 と 08-12 以降は未収集）。評価窓はこの中に収める。
訓練期間の 05-21〜05-29 は欠損のままでよい（LightGBM は NaN を扱える）。

使い方:
    python scripts/wf_extra_features.py
"""
from __future__ import annotations

import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from src.features.builder import (build_features, FEATURE_COLS,   # noqa: E402
                                  EXTRA_FEATURE_COLS)
from src.models import plackett_luce as pl                        # noqa: E402
from src.ingestion.database import get_engine, init_db            # noqa: E402
from src.utils.helpers import load_config                         # noqa: E402

TRAIN_FROM = "2026-01-01"
# 評価窓が 05-30〜08-11 に収まる打ち切り（1日も重ねない）
CUTOFFS = ["2026-05-30", "2026-06-19", "2026-07-09", "2026-07-29"]
WINDOWS = {"2026-05-30": ("2026-05-31", "2026-06-19"),
           "2026-06-19": ("2026-06-20", "2026-07-09"),
           "2026-07-09": ("2026-07-10", "2026-07-29"),
           "2026-07-29": ("2026-07-30", "2026-08-11")}
KEEP = 0.742

BASE = [c for c in FEATURE_COLS if c not in EXTRA_FEATURE_COLS]
WITH = BASE + EXTRA_FEATURE_COLS


def _fit(df, cols, seed):
    X = df[cols].apply(pd.to_numeric, errors="coerce").values.astype(float)
    y = (4 - df["arrival_order"].astype(int).clip(1, 4)).clip(0, 3).values
    groups = df.groupby("race_id", sort=False).size().values
    med = np.nanmedian(X, axis=0)
    X = np.where(np.isnan(X), med, X)
    r = lgb.LGBMRanker(
        objective="lambdarank", n_estimators=500, learning_rate=0.05,
        max_depth=6, num_leaves=31, subsample=0.8, colsample_bytree=0.8,
        min_child_samples=20, random_state=seed, n_jobs=-1, verbose=-1,
        label_gain=[0, 1, 3, 7])
    r.fit(X, y, group=groups)
    r._medians = med
    return r


def train_pair(cutoff, seed):
    """同じ訓練データから 34項目版 と 47項目版 を作る。"""
    df = build_features(TRAIN_FROM, cutoff, include_target=True)
    df = df.dropna(subset=["arrival_order"])
    df = df[df["arrival_order"] > 0]
    if df.empty:
        return None, None, 0
    df = df.sort_values(["race_date", "race_id", "boat_no"]).reset_index(drop=True)
    return _fit(df, BASE, seed), _fit(df, WITH, seed), df["race_id"].nunique()


def _scores(model, df, cols):
    X = df[cols].apply(pd.to_numeric, errors="coerce").values.astype(float)
    X = np.where(np.isnan(X), model._medians, X)
    return model.predict(X)


def evaluate(m_base, m_with, d1, d2):
    """同じレースの上で両モデルの1点と全組合せの確率を返す。"""
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    if df.empty:
        return [], []
    df = df.assign(s_base=_scores(m_base, df, BASE),
                   s_with=_scores(m_with, df, WITH))

    from sqlalchemy import text
    with get_engine().connect() as conn:
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r "
            "ON r.id=o.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND o.is_final=1 AND o.odds>0 AND o.bet_type='nirenfuku'"),
            {"d1": d1, "d2": d2}).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no,rr.arrival_order FROM race_results rr "
            "JOIN races r ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order IS NOT NULL"), {"d1": d1, "d2": d2}).fetchall()

    nf = defaultdict(dict)
    for rid, cb, o in od:
        nf[int(rid)][str(cb)] = float(o)
    order = defaultdict(dict)
    for rid, bn, ao in res:
        order[int(rid)][int(ao)] = str(bn)

    picks, combos = [], []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        o, fin = nf.get(rid, {}), order.get(rid, {})
        if len(o) != 15 or 1 not in fin or 2 not in fin:
            continue
        rows = list(grp.itertuples())
        if len(rows) < 6:
            continue
        top2 = {fin[1], fin[2]}
        ex_b = pl.to_exp_scores({int(r.boat_no): float(r.s_base) for r in rows}, temperature=temp)
        ex_w = pl.to_exp_scores({int(r.boat_no): float(r.s_with) for r in rows}, temperature=temp)
        pb, pw = {}, {}
        for cb in o:
            a, b = sorted(int(x) for x in cb.split("-"))
            pb[cb] = pl.joint_prob_nirenfuku(ex_b, a, b)
            pw[cb] = pl.joint_prob_nirenfuku(ex_w, a, b)
            hit = 1 if set(cb.split("-")) == top2 else 0
            combos.append((pb[cb], pw[cb], o[cb], hit))
        cb_b, cb_w = max(pb, key=pb.get), max(pw, key=pw.get)
        picks.append({
            "base_ret": o[cb_b] if set(cb_b.split("-")) == top2 else 0.0,
            "with_ret": o[cb_w] if set(cb_w.split("-")) == top2 else 0.0,
            "base_hit": int(set(cb_b.split("-")) == top2),
            "with_hit": int(set(cb_w.split("-")) == top2),
            "same": cb_b == cb_w,
        })
    return picks, combos


def logloss(p, hit):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -(math.log(p) if hit else math.log(1 - p))


def paired_ci(diffs, T=2000):
    """対応のある差のブートストラップ区間。"""
    if len(diffs) < 30:
        return None, None
    random.seed(0)
    v = []
    for _ in range(T):
        s = [random.choice(diffs) for _ in diffs]
        v.append(sum(s) / len(s))
    v.sort()
    return v[int(.025 * T)], v[int(.975 * T)]


def report(label, picks, combos):
    n = len(picks)
    if not n:
        print(f"  {label}: レースなし")
        return
    b_roi = sum(x["base_ret"] for x in picks) / n * 100
    w_roi = sum(x["with_ret"] for x in picks) / n * 100
    b_hit = sum(x["base_hit"] for x in picks) / n * 100
    w_hit = sum(x["with_hit"] for x in picks) / n * 100
    d = [x["with_ret"] - x["base_ret"] for x in picks]
    lo, hi = paired_ci(d)
    ci = f" [95% {lo * 100:+.1f}〜{hi * 100:+.1f}pt]" if lo is not None else ""
    same = sum(1 for x in picks if x["same"]) / n * 100
    print(f"  {label}  {n}レース  買う1点が同じ {same:.0f}%")
    print(f"     34項目  的中{b_hit:5.1f}%  回収{b_roi:6.1f}%")
    print(f"     47項目  的中{w_hit:5.1f}%  回収{w_roi:6.1f}%   差 {w_roi - b_roi:+.1f}pt{ci}")
    lb = sum(logloss(p, h) for p, _, _, h in combos) / len(combos)
    lw = sum(logloss(p, h) for _, p, _, h in combos) / len(combos)
    dd = [logloss(pw, h) - logloss(pb, h) for pb, pw, _, h in combos]
    llo, lhi = paired_ci(dd)
    lci = f" [95% {llo:+.5f}〜{lhi:+.5f}]" if llo is not None else ""
    print(f"     対数損失 {lb:.5f} → {lw:.5f}  差 {lw - lb:+.5f}{lci}"
          f"  {'（47項目が良い）' if lw < lb else '（34項目が良い）'}")


def main():
    init_db(load_config())
    cfg = load_config()
    seed = cfg["model"].get("random_state", 42)
    print(f"34項目 vs 47項目（+{len(EXTRA_FEATURE_COLS)}: 展示・気象）")
    print(f"打ち切り {CUTOFFS}")
    print("評価窓は直前情報の被覆が揃う 2026-05-30〜08-11 に収めてある")
    print()

    all_picks, all_combos = {}, {}
    for cu in CUTOFFS:
        d1, d2 = WINDOWS[cu]
        mb, mw, ntr = train_pair(cu, seed)
        if mb is None:
            print(f"{cu}: 訓練データなし")
            continue
        picks, combos = evaluate(mb, mw, d1, d2)
        all_picks[cu], all_combos[cu] = picks, combos
        print(f"打ち切り {cu}（訓練 {ntr}レース）→ 予測 {d1}〜{d2}")
        report("この窓", picks, combos)
        print()

    half = len(CUTOFFS) // 2
    for name, cus in (("窓A（前半）", CUTOFFS[:half]), ("窓B（後半）", CUTOFFS[half:])):
        p = [x for c in cus for x in all_picks.get(c, [])]
        k = [x for c in cus for x in all_combos.get(c, [])]
        if p:
            print(f"=== {name} {cus} ===")
            report("合計", p, k)
            print()

    print("判定の目安: **両窓とも回収率が上がって初めて採用**。")
    print("片窓だけなら偽陽性（2026-08-13 の対数損失比較がまさにそれだった）。")


if __name__ == "__main__":
    main()
