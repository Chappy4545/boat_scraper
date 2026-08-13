"""2つのモデルを同じ組合せの上で対にして比べる。

集計値どうしを引き算すると、差が誤差なのか実力なのか分からない。
同じ行に両モデルの予測を並べ、行ごとの対数損失の差を取れば、
そのばらつきから標準誤差が出せる（対応のある比較）。

用途: 特徴量を足したときに本当に良くなったのかを判定する。
市場との差も同じ形で出すので、「市場にどこまで近づいたか」も分かる。

**必ず期間を区切って訓練したモデルどうしを、その期間より後で比べること。**

使い方:
    python scripts/compare_two_models.py <A.joblib> <B.joblib> <from> <to>
      A: 基準（例 34項目）  B: 比較対象（例 47項目）
    B 側だけ列が多い場合は BOAT_EXTRA_FEATURES=1 を付けて実行する
    （両モデルとも同じ環境変数で読み込むため、A も 47 列で解釈されないよう
      A は 34 項目のままにできない。そのため本スクリプトは列数の違いを
      モデル側の n_features_ から判定し、各モデルに必要な列だけを渡す）。
"""
from __future__ import annotations

import logging
import math
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features.builder import (   # noqa: E402
    build_features, FEATURE_COLS, EXTRA_FEATURE_COLS,
)
from src.models import plackett_luce as pl        # noqa: E402
from src.ingestion.database import get_engine     # noqa: E402
from src.utils.helpers import load_config         # noqa: E402

BT = "nirenfuku"
N_COMB = 15
BASE_COLS = [c for c in FEATURE_COLS if c not in EXTRA_FEATURE_COLS]


def _ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _cols_for(model) -> list[str]:
    """モデルが期待する列数から、渡すべき列を決める。"""
    n = getattr(model, "n_features_", None) or getattr(model, "n_features_in_", None)
    if n is None:
        return list(FEATURE_COLS)
    if n == len(BASE_COLS):
        return BASE_COLS
    if n == len(BASE_COLS) + len(EXTRA_FEATURE_COLS):
        return BASE_COLS + EXTRA_FEATURE_COLS
    raise SystemExit(f"モデルの期待する列数 {n} に一致する組み合わせがありません "
                     f"（基本 {len(BASE_COLS)} / 拡張込み "
                     f"{len(BASE_COLS) + len(EXTRA_FEATURE_COLS)}）")


def scores_for(model, df: pd.DataFrame) -> np.ndarray:
    cols = _cols_for(model)
    X = df[cols].apply(pd.to_numeric, errors="coerce")
    return model.predict(X.fillna(X.median()).values)


def main() -> None:
    pa, pb, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    A, B = joblib.load(pa), joblib.load(pb)
    print(f"A: {Path(pa).parent.name}  ({len(_cols_for(A))}項目)")
    print(f"B: {Path(pb).parent.name}  ({len(_cols_for(B))}項目)\n")

    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    df = df.assign(_sa=scores_for(A, df), _sb=scores_for(B, df))

    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": [BT]}
    with get_engine().connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.combination FROM payouts p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r ON r.id=o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_final=1 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()

    hits: dict[int, set] = defaultdict(set)
    for rid, cb in pay:
        hits[int(rid)].add(str(cb))
    odds: dict[int, dict] = defaultdict(dict)
    for rid, cb, o in od:
        if o and o > 0:
            odds[int(rid)][str(cb)] = float(o)

    rows = []
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        if len(odds.get(rid, {})) != N_COMB or rid not in hits:
            continue
        if len(g) != 6:
            continue
        pa_ = {c["combination"]: float(c["model_prob"]) for c in pl.all_bet_probs(
            {int(b): float(s) for b, s in zip(g["boat_no"], g["_sa"])},
            temperature=temp).get(BT, [])}
        pb_ = {c["combination"]: float(c["model_prob"]) for c in pl.all_bet_probs(
            {int(b): float(s) for b, s in zip(g["boat_no"], g["_sb"])},
            temperature=temp).get(BT, [])}
        inv = {cb: 1.0 / o for cb, o in odds[rid].items()}
        tot = sum(inv.values())
        won = hits[rid]
        for cb, o in odds[rid].items():
            if cb in pa_ and cb in pb_:
                rows.append({"a": pa_[cb], "b": pb_[cb], "k": inv[cb] / tot,
                             "odds": o, "y": 1 if cb in won else 0})

    R = pd.DataFrame(rows)
    if R.empty:
        print("該当データなし")
        return
    la, lb, lk = (_ll(R["a"].values, R["y"].values),
                  _ll(R["b"].values, R["y"].values),
                  _ll(R["k"].values, R["y"].values))
    n = len(R)
    print(f"=== {d1}〜{d2}  {n:,}組合せ ===\n")
    print(f"対数損失   A {la.mean():.5f}   B {lb.mean():.5f}   市場 {lk.mean():.5f}\n")

    def paired(d: np.ndarray, base: float, label: str) -> None:
        m, se = d.mean(), d.std(ddof=1) / math.sqrt(len(d))
        rel, rel2 = m / base * 100, 2 * se / base * 100
        sig = "有意" if abs(m) > 2 * se else "誤差の範囲"
        print(f"  {label:26} {rel:+6.2f}% ±{rel2:.2f}   {sig}")

    print("対にした差（正なら後者が良い）")
    paired(la - lb, la.mean(), "B は A より")
    paired(lk - lb, lk.mean(), "B は市場より")
    paired(lk - la, lk.mean(), "A は市場より")

    print("\n回収率（model_prob>=0.30 かつ EV>=1.2、賭け金一律100円）")
    for lbl, col in (("A", "a"), ("B", "b")):
        sel = R[(R[col] >= 0.30) & (R[col] * R["odds"] >= 1.2)]
        if len(sel) < 30:
            print(f"  {lbl}: {len(sel)}本（少なすぎ）")
            continue
        hr = sel["y"].mean()
        roi = (sel["y"] * sel["odds"]).sum() / len(sel)
        avg = (sel["y"] * sel["odds"]).sum() / max(sel["y"].sum(), 1)
        se = math.sqrt(hr * (1 - hr)) * avg / math.sqrt(len(sel))
        print(f"  {lbl}: {len(sel):>4,}本  的中{hr * 100:5.1f}%  "
              f"回収{roi * 100:5.1f}% ±{se * 100 * 2:.0f}")


if __name__ == "__main__":
    main()
