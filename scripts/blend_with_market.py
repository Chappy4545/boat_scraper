"""市場を土台にして、モデルで補正する。

モデル単独は市場に負けている（未見データで -2.5〜3.0%）。だが市場と
モデルは別の情報を見ているので、混ぜれば市場単独より良くなりうる。
これは賭けの世界では標準的な考え方で、「市場が最良の単独推定量、
自分のモデルは残差を少し削るもの」と扱う。

    p_blend = w * p_model + (1-w) * p_market      （確率空間で線形に混合）

**混合比 w を測るのと同じデータで選んではいけない。** 選んだ時点で
そのデータに合わせ込んでいる。ここでは片方の窓で w を決め、
もう片方の窓でその w を使って測る。

回収率も見る。対数損失が良くなっても、買える買い目が出るとは限らない:
    EV = p_blend / (p_market * 1.348)
なので EV>=1.2 には p_blend/p_market >= 1.618 が要る。市場に寄せるほど
この比は 1 に近づき、賭ける対象が消える。精度と本数は逆を向く。

使い方:
    python scripts/blend_with_market.py <選定窓ranker> <from> <to> \
                                        <検証窓ranker> <from> <to>
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
WEIGHTS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]


def _ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _cols_for(model) -> list[str]:
    n = getattr(model, "n_features_", None) or getattr(model, "n_features_in_", None)
    if n == len(BASE_COLS) + len(EXTRA_FEATURE_COLS):
        return BASE_COLS + EXTRA_FEATURE_COLS
    return BASE_COLS


def load(ranker_path: str, d1: str, d2: str) -> pd.DataFrame:
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    model = joblib.load(ranker_path)
    cols = _cols_for(model)

    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    X = df[cols].apply(pd.to_numeric, errors="coerce")
    df = df.assign(_s=model.predict(X.fillna(X.median()).values))

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
        if len(odds.get(rid, {})) != N_COMB or rid not in hits or len(g) != 6:
            continue
        probs = {c["combination"]: float(c["model_prob"]) for c in pl.all_bet_probs(
            {int(b): float(s) for b, s in zip(g["boat_no"], g["_s"])},
            temperature=temp).get(BT, [])}
        inv = {cb: 1.0 / o for cb, o in odds[rid].items()}
        tot = sum(inv.values())
        won = hits[rid]
        for cb, o in odds[rid].items():
            if cb in probs:
                rows.append({"pm": probs[cb], "pk": inv[cb] / tot, "odds": o,
                             "y": 1 if cb in won else 0})
    return pd.DataFrame(rows)


def blend(R: pd.DataFrame, w: float) -> np.ndarray:
    return w * R["pm"].values + (1 - w) * R["pk"].values


def main() -> None:
    r1, a1, b1, r2, a2, b2 = sys.argv[1:7]

    print("【1】選定窓 — ここで混合比を決める")
    S = load(r1, a1, b1)
    print(f"  {a1}〜{b1}  {len(S):,}組合せ")
    base = _ll(S["pk"].values, S["y"].values).mean()
    best_w, best_ll = None, float("inf")
    for w in WEIGHTS:
        ll = _ll(blend(S, w), S["y"].values).mean()
        mark = ""
        if ll < best_ll:
            best_ll, best_w, mark = ll, w, ""
        print(f"    モデル{w * 100:3.0f}% : {ll:.5f}  ({(base - ll) / base * 100:+.2f}% 対市場)")
    print(f"  → 採用: モデル {best_w * 100:.0f}%\n")

    print("【2】検証窓 — 決めた比率をそのまま当てる")
    V = load(r2, a2, b2)
    y = V["y"].values
    llk = _ll(V["pk"].values, y)
    llb = _ll(blend(V, best_w), y)
    llm = _ll(V["pm"].values, y)
    print(f"  {a2}〜{b2}  {len(V):,}組合せ")
    print(f"    市場単独   {llk.mean():.5f}")
    print(f"    モデル単独 {llm.mean():.5f}")
    print(f"    混合({best_w * 100:.0f}%)  {llb.mean():.5f}")

    d = llk - llb
    m, se = d.mean(), d.std(ddof=1) / math.sqrt(len(d))
    sig = "有意" if abs(m) > 2 * se else "誤差の範囲"
    print(f"    → 混合は市場より {m / llk.mean() * 100:+.2f}% "
          f"±{2 * se / llk.mean() * 100:.2f}  {sig}\n")

    print("【3】その混合確率で賭けたらどうなるか（検証窓・賭け金一律100円）")
    print(f"    {'ルール':28}{'本数':>7}{'的中率':>8}{'回収率':>9}{'±2SE':>7}")
    pb = blend(V, best_w)
    ratio = pb / np.maximum(V["pk"].values, 1e-9)
    ev = pb * V["odds"].values
    rules = [("混合EV >= 1.2", ev >= 1.2),
             ("混合EV >= 1.5", ev >= 1.5),
             ("混合/市場 >= 1.5", ratio >= 1.5),
             ("混合/市場 >= 2.0", ratio >= 2.0),
             ("混合p>=0.30 & EV>=1.2", (pb >= 0.30) & (ev >= 1.2))]
    for label, mask in rules:
        sel = V[mask]
        n = len(sel)
        if n < 30:
            print(f"    {label:28}{n:>7,}  （少なすぎて判断不能）")
            continue
        hr = sel["y"].mean()
        roi = (sel["y"] * sel["odds"]).sum() / n
        avg = (sel["y"] * sel["odds"]).sum() / max(sel["y"].sum(), 1)
        se_roi = math.sqrt(hr * (1 - hr)) * avg / math.sqrt(n)
        print(f"    {label:28}{n:>7,}{hr * 100:>7.1f}%{roi * 100:>8.1f}%"
              f"{se_roi * 200:>6.0f}%")
    print("\n    ※ 100%を超えないと賭ける意味はない（控除率25.8%）")


if __name__ == "__main__":
    main()
