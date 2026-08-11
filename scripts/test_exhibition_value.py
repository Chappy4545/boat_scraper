"""展示タイム（直前情報）に予測力があるかを測る。

市場（オッズ）はモデルより正確（対数損失 0.194 vs 0.197）。
市場は展示タイムを見ているが、こちらは 2026-05-21 に取得を止めて以来
使っていない。相手の持つ情報を捨てているなら、そこが最大の伸びしろになる。

before_info がある期間（1/1〜5/21, 21,733レース）で、
展示タイムの有無だけを変えて LambdaRank を訓練し、
同じ未見期間で比べる。

使い方: python scripts/test_exhibition_value.py
"""
from __future__ import annotations

import logging
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.features.builder import build_features, FEATURE_COLS
from src.ingestion.database import get_engine
from src.utils.helpers import load_config

TRAIN_TO = "2026-04-15"          # ここまでで訓練
TEST_FROM, TEST_TO = "2026-04-16", "2026-05-21"   # 未見期間（before_info がある範囲）


def add_exhibition(df):
    """展示タイムは build_features が既に結合済み（FEATURE_COLS から
    外されているだけ）。ここでは使える形に整える。"""
    m = df.copy()
    m["exhibition_time"] = pd.to_numeric(m["exhibition_time"], errors="coerce")
    # レース内での相対値にする（絶対値は場や水面で意味が変わる）
    g = m.groupby("race_id")["exhibition_time"]
    m["ex_time_z"] = (m["exhibition_time"] - g.transform("mean")) / g.transform("std").replace(0, np.nan)
    m["ex_rank"] = g.rank(method="min")          # 速い順
    m["tilt_v"] = pd.to_numeric(m["tilt"], errors="coerce")
    return m


def train_eval(train, test, cols, label):
    import lightgbm as lgb
    tr = train.sort_values("race_id")
    te = test.sort_values("race_id")
    Xtr = tr[cols].apply(pd.to_numeric, errors="coerce")
    Xte = te[cols].apply(pd.to_numeric, errors="coerce")
    med = Xtr.median()
    Xtr, Xte = Xtr.fillna(med), Xte.fillna(med)
    # 着順を relevance に（1着=5 ... 6着=0）
    ytr = (6 - tr["_rank"]).clip(lower=0).astype(int)
    grp_tr = tr.groupby("race_id", sort=False).size().values

    m = lgb.LGBMRanker(
        objective="lambdarank", n_estimators=300, learning_rate=0.05,
        num_leaves=31, min_child_samples=30, random_state=42, verbose=-1,
    )
    m.fit(Xtr, ytr, group=grp_tr)

    te = te.assign(_s=m.predict(Xte))
    top = te.loc[te.groupby("race_id")["_s"].idxmax()]
    acc = (top["_rank"] == 1).mean()

    # 実際に買うのは2連複なので、そちらでも測る。
    # Plackett-Luce で全15組合せの確率を出し、確率30%以上の的中率を見る。
    from src.models import plackett_luce as pl
    hit = tot = 0
    ll = []
    for rid, g in te.groupby("race_id", sort=False):
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_s"])}
        if len(scores) < 6:
            continue
        actual = set(g.loc[g["_rank"] <= 2, "boat_no"].astype(int))
        if len(actual) != 2:
            continue
        key = f"{min(actual)}-{max(actual)}"
        for c in pl.all_bet_probs(scores, temperature=1.0).get("nirenfuku", []):
            p = float(c["model_prob"])
            y = 1 if c["combination"] == key else 0
            ll.append(-(y * np.log(max(p, 1e-9)) + (1 - y) * np.log(max(1 - p, 1e-9))))
            if p >= 0.30:
                tot += 1
                hit += y
    rate = hit / tot if tot else 0
    print(f"  {label:<34} 特徴量{len(cols):>3}個  1着 {acc*100:5.2f}%  "
          f"2連複30%帯 {rate*100:5.2f}%({tot}本)  対数損失 {np.mean(ll):.5f}")
    return acc, rate, float(np.mean(ll))


def main():
    engine = get_engine()
    print(f"訓練: 〜{TRAIN_TO} / 検証: {TEST_FROM}〜{TEST_TO}（未見）")

    df = build_features(None, TEST_TO, include_target=True).dropna(subset=["target_win"])
    # 着順を復元（target_win/top2/top3 から）
    df["_rank"] = np.where(df["target_win"] == 1, 1,
                    np.where(df["target_top2"] == 1, 2,
                      np.where(df["target_top3"] == 1, 3, 4)))

    df = add_exhibition(df)
    df = df[df["exhibition_time"].notna()]
    print(f"展示タイムがある行: {len(df):,} / {df['race_id'].nunique():,} レース")

    train = df[df["race_date"].astype(str) <= TRAIN_TO]
    test = df[(df["race_date"].astype(str) >= TEST_FROM)]
    print(f"訓練 {train['race_id'].nunique():,} レース / 検証 {test['race_id'].nunique():,} レース\n")

    base = [c for c in FEATURE_COLS if c in df.columns]
    print("【比較】")
    a1, r1, l1 = train_eval(train, test, base, "現行30特徴量")
    a2, r2, l2 = train_eval(train, test, base + ["ex_time_z", "ex_rank", "tilt_v"], "＋展示タイム・チルト")
    print(f"\n  1着的中率      {(a2-a1)*100:+.2f} ポイント")
    print(f"  2連複30%帯     {(r2-r1)*100:+.2f} ポイント")
    print(f"  対数損失       {l2-l1:+.5f}（マイナスなら改善）")


if __name__ == "__main__":
    main()
