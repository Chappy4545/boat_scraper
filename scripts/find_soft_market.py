"""市場が甘くなる場所を探す。

全体ではモデルは市場に負けている（未見データで対数損失 3.1% 劣位）。
だが市場の正確さは一様ではない。売上が小さいレースはオッズが荒く、
賭ける人の質も揃わないので、そこだけモデルが勝てる可能性がある。
利益が出るとすればこの一点しか残っていない。

**必ず期間を区切って訓練したモデルで走らせること。** 本番モデルは
全データで訓練されているため、どの期間で測っても in-sample になる。

比較は組合せごとの対数損失の差を対にして取る（paired）。
    diff = 市場の対数損失 - モデルの対数損失
    正ならモデルの勝ち。平均と標準誤差を出すので、
    「差が誤差の範囲か」がその場で分かる。
集計値どうしを引き算するより検出力が高く、区分ごとの本数が少なくても効く。

使い方:
    python scripts/find_soft_market.py <ranker> <from> <to> [--csv out.csv]
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

from src.features.builder import build_features, FEATURE_COLS   # noqa: E402
from src.models import plackett_luce as pl                      # noqa: E402
from src.ingestion.database import get_engine                   # noqa: E402
from src.utils.helpers import load_config                       # noqa: E402

BT = "nirenfuku"
N_COMB = 15
MIN_COMBOS = 1500          # この本数未満の区分は判断しない


def _logloss(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def load_rows(ranker_path: str, d1: str, d2: str) -> pd.DataFrame:
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    ranker = joblib.load(ranker_path)

    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    df = df.assign(_score=ranker.predict(X.fillna(X.median()).values))

    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": [BT]}
    engine = get_engine()
    with engine.connect() as conn:
        pay = conn.execute(text(
            "SELECT p.race_id,p.combination FROM payouts p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        od = conn.execute(text(
            "SELECT o.race_id,o.combination,o.odds FROM odds o JOIN races r ON r.id=o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_final=1 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        meta = conn.execute(text(
            "SELECT r.id, r.stadium_id, r.race_no, r.grade, r.is_night, s.name "
            "FROM races r JOIN stadiums s ON s.id = r.stadium_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2"
        ), {"d1": d1, "d2": d2}).fetchall()

    hits: dict[int, set] = defaultdict(set)
    for rid, cb in pay:
        hits[int(rid)].add(str(cb))
    odds: dict[int, dict] = defaultdict(dict)
    for rid, cb, o in od:
        if o and o > 0:
            odds[int(rid)][str(cb)] = float(o)
    info = {int(m[0]): {"stadium_id": m[1], "race_no": m[2], "grade": m[3],
                        "is_night": bool(m[4]), "stadium": m[5]} for m in meta}

    rows = []
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        # 15通り揃っていないレースは市場確率を正規化できない
        if len(odds.get(rid, {})) != N_COMB or rid not in hits or rid not in info:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) != 6:
            continue
        probs = {c["combination"]: float(c["model_prob"])
                 for c in pl.all_bet_probs(scores, temperature=temp).get(BT, [])}

        inv = {cb: 1.0 / o for cb, o in odds[rid].items()}
        overround = sum(inv.values())          # 1/(1-控除率)。標準は約1.348
        mkt = {cb: v / overround for cb, v in inv.items()}
        won = hits[rid]
        m = info[rid]
        # そのレースの市場の形。売上が薄いほどオッズは粗くなる。
        fav_mp = max(mkt.values())
        n_distinct = len({round(o, 1) for o in odds[rid].values()})

        for cb, o in odds[rid].items():
            if cb not in probs:
                continue
            rows.append({
                "race_id": rid, "combination": cb,
                "pm": probs[cb], "pk": mkt[cb], "odds": o,
                "y": 1 if cb in won else 0,
                "overround": overround, "fav_mp": fav_mp, "n_distinct": n_distinct,
                **m,
            })
    return pd.DataFrame(rows)


def report(R: pd.DataFrame, by: str, label: str, order=None) -> list[dict]:
    """区分ごとに「モデル - 市場」の対数損失差を対で測る。"""
    print(f"\n── {label} ──")
    print(f"{'区分':16}{'レース':>7}{'組合せ':>8}{'優位':>9}{'±2SE':>8}{'判定':>10}"
          f"{'R5本数':>8}{'R5回収':>8}")
    out = []
    keys = order if order is not None else sorted(R[by].dropna().unique())
    for k in keys:
        sub = R[R[by] == k]
        if len(sub) < MIN_COMBOS:
            continue
        d = sub["ll_market"].values - sub["ll_model"].values   # 正=モデルの勝ち
        mean, se = d.mean(), d.std(ddof=1) / math.sqrt(len(d))
        rel = mean / sub["ll_market"].mean() * 100
        rel_se = 2 * se / sub["ll_market"].mean() * 100
        verdict = ("モデル有利" if mean - 2 * se > 0 else
                   "市場有利" if mean + 2 * se < 0 else "差なし")
        bet = sub[(sub["pm"] >= 0.30) & (sub["pm"] * sub["odds"] >= 1.2)]
        if len(bet):
            roi = (bet["y"] * bet["odds"]).sum() / len(bet) * 100
            roi_s = f"{roi:7.0f}%"
        else:
            roi_s = "      -"
        print(f"{str(k)[:15]:16}{sub['race_id'].nunique():>7,}{len(sub):>8,}"
              f"{rel:>+8.1f}%{rel_se:>7.1f}%{verdict:>10}{len(bet):>8,}{roi_s:>8}")
        out.append({"segment": by, "key": k, "races": sub["race_id"].nunique(),
                    "combos": len(sub), "rel": rel, "rel_se": rel_se,
                    "verdict": verdict, "n_bets": len(bet),
                    "roi": roi if len(bet) else None})
    return out


def main() -> None:
    ranker, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    R = load_rows(ranker, d1, d2)
    if R.empty:
        print("該当データなし")
        return

    R["ll_model"] = _logloss(R["pm"].values, R["y"].values)
    R["ll_market"] = _logloss(R["pk"].values, R["y"].values)

    d = R["ll_market"].values - R["ll_model"].values
    mean, se = d.mean(), d.std(ddof=1) / math.sqrt(len(d))
    rel = mean / R["ll_market"].mean() * 100
    print(f"=== {d1}〜{d2}  {R['race_id'].nunique():,}レース / {len(R):,}組合せ ===")
    print(f"全体: モデルの優位 {rel:+.1f}% ±{2 * se / R['ll_market'].mean() * 100:.1f}"
          f"（正ならモデルの勝ち）")

    # 市場の薄さを表す量で切る
    R["_over"] = pd.cut(R["overround"], [0, 1.30, 1.34, 1.36, 1.40, 9],
                        labels=["〜1.30", "1.30-1.34", "1.34-1.36", "1.36-1.40", "1.40〜"])
    R["_rno"] = pd.cut(R["race_no"], [0, 4, 8, 12], labels=["1-4R", "5-8R", "9-12R"])
    R["_fav"] = pd.cut(R["fav_mp"], [0, .15, .25, .35, 1],
                       labels=["〜15%(混戦)", "15-25%", "25-35%", "35%〜(本命堅い)"])
    R["_dst"] = pd.cut(R["n_distinct"], [0, 12, 13, 14, 15],
                       labels=["〜12(粗い)", "13", "14", "15(細かい)"])
    R["_grade"] = R["grade"].fillna("不明")
    R["_night"] = R["is_night"].map({True: "ナイター", False: "デイ"})

    segs = []
    segs += report(R, "_over", "控除率の実測 sum(1/odds)  ※標準1.348・大きいほど売上が薄い")
    segs += report(R, "_rno", "レース番号  ※後半ほど売上が大きい",
                   order=["1-4R", "5-8R", "9-12R"])
    segs += report(R, "_fav", "本命の堅さ（市場が見た1番人気の確率）",
                   order=["〜15%(混戦)", "15-25%", "25-35%", "35%〜(本命堅い)"])
    segs += report(R, "_dst", "オッズの粒度（15通り中の異なる値の数）",
                   order=["〜12(粗い)", "13", "14", "15(細かい)"])
    segs += report(R, "_grade", "グレード")
    segs += report(R, "_night", "開催時間帯")
    segs += report(R, "stadium", "会場")

    print("\n=== モデル有利と出た区分（次の窓で確認すべき候補）===")
    hit = [s for s in segs if s["verdict"] == "モデル有利"]
    if not hit:
        print("  なし")
    for s in sorted(hit, key=lambda x: -x["rel"]):
        print(f"  {s['segment']:8} {str(s['key'])[:16]:18} "
              f"優位{s['rel']:+.1f}% ±{s['rel_se']:.1f}  {s['combos']:,}組合せ")

    if "--csv" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--csv") + 1])
        pd.DataFrame(segs).to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n書き出し: {out}")


if __name__ == "__main__":
    main()
