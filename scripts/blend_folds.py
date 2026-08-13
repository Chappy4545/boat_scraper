"""市場×モデルの混合を、期間を分けて何度も測る。

2つの窓で測ったら答えが割れた（+0.14% と +1.18%）。窓が2つでは
どちらが実態か決まらないので、測る回数を増やす。

4/30 で区切ったモデルは 5/1 以降すべてが未見データなので、その一本で
5月〜8月を通して走らせ、期間で分割すれば独立した測定が複数取れる。
モデルは時間が経つほど古びるが、それは実運用と同じ条件でもある。

混合比は事前に固定する（データを見てから選ばない）。
    p_blend = w * p_model + (1-w) * p_market

回収率は的中がまれな買い目に支配されるため、平均だけでなく
「その期間で最大の払戻が全体の何割を占めるか」も出す。
1本の大穴で勝っているだけなら、その数字は将来の指針にならない。

使い方:
    python scripts/blend_folds.py <ranker> <from> <to> [--folds 4] [--w 0.3]
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


def load(ranker_path: str, d1: str, d2: str) -> pd.DataFrame:
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    model = joblib.load(ranker_path)
    n = getattr(model, "n_features_", None) or getattr(model, "n_features_in_", None)
    cols = (BASE_COLS + EXTRA_FEATURE_COLS
            if n == len(BASE_COLS) + len(EXTRA_FEATURE_COLS) else BASE_COLS)

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
    for (race_id, rdate), g in df.groupby(["race_id", "race_date"], sort=False):
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
                rows.append({"date": str(rdate), "race_id": rid,
                             "pm": probs[cb], "pk": inv[cb] / tot, "odds": o,
                             "y": 1 if cb in won else 0})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def main() -> None:
    ranker, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    folds = int(sys.argv[sys.argv.index("--folds") + 1]) if "--folds" in sys.argv else 4
    w = float(sys.argv[sys.argv.index("--w") + 1]) if "--w" in sys.argv else 0.3

    R = load(ranker, d1, d2)
    dates = sorted(R["date"].unique())
    edges = [dates[int(len(dates) * i / folds)] for i in range(folds)] + [None]
    print(f"=== {d1}〜{d2}  {R['race_id'].nunique():,}レース / {len(R):,}組合せ "
          f"（混合比はモデル {w * 100:.0f}% に固定）===\n")
    print(f"{'期間':24}{'組合せ':>8}{'混合−市場':>11}{'±2SE':>8}{'判定':>10}"
          f"{'賭数':>7}{'回収率':>8}{'最大1本の寄与':>13}")

    rows = []
    for i in range(folds):
        lo, hi = edges[i], edges[i + 1]
        sub = R[(R["date"] >= lo) & ((R["date"] < hi) if hi else True)]
        rows.append((f"{lo}〜{sub['date'].max()}", sub))
    rows.append(("全期間", R))

    for label, sub in rows:
        y = sub["y"].values
        llk = _ll(sub["pk"].values, y)
        llb = _ll(w * sub["pm"].values + (1 - w) * sub["pk"].values, y)
        d = llk - llb
        m, se = d.mean(), d.std(ddof=1) / math.sqrt(len(d))
        rel, rel2 = m / llk.mean() * 100, 2 * se / llk.mean() * 100
        verdict = ("混合が上" if m - 2 * se > 0 else
                   "市場が上" if m + 2 * se < 0 else "差なし")

        pb = w * sub["pm"].values + (1 - w) * sub["pk"].values
        mask = pb * sub["odds"].values >= 1.2
        sel = sub[mask]
        if len(sel) >= 30:
            pays = (sel["y"] * sel["odds"]).values
            roi = pays.sum() / len(sel) * 100
            share = pays.max() / pays.sum() * 100 if pays.sum() > 0 else 0
            bet_s, roi_s, sh_s = f"{len(sel):,}", f"{roi:.0f}%", f"{share:.0f}%"
        else:
            bet_s, roi_s, sh_s = f"{len(sel):,}", "-", "-"
        sep = "─" if label == "全期間" else ""
        if sep:
            print("─" * 89)
        print(f"{label:24}{len(sub):>8,}{rel:>+10.2f}%{rel2:>7.2f}%{verdict:>10}"
              f"{bet_s:>7}{roi_s:>8}{sh_s:>13}")

    print("\n※ 最大1本の寄与 = その期間の払戻合計に占める、最も高配当だった1本の割合。")
    print("   これが大きいほど、回収率はまぐれ当たりで決まっている。")


if __name__ == "__main__":
    main()
