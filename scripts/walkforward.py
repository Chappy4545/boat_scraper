"""ウォークフォワードで「未見データの予測」を大量に貯める。

なぜ必要か
----------
2026-08-25 時点で検証に使えたのは 758 レース（8/19〜23）しか無く、
どの仮説も「母数が足りず決められない」で終わっていた。一方 DB には
確定オッズ・着順・出走表が揃ったレースが **14,997**（5月以降）ある。
各時点で「その日より後を一切見ていないモデル」の予測を作り直せば、
**測定能力が20倍**になる。

やること
--------
    5/01までで訓練 → 5/02〜5/21 を予測して保存
    5/21までで訓練 → 5/22〜6/10 を予測して保存
      … 打ち切りをずらしながら8月まで

⚠️ リーク対策（過去にバックテストが壊れた経緯: project_backtest_leak）
  - 訓練は打ち切り日**まで**。予測はその**翌日以降**。1日も重ねない
  - 本番モデル(data/processed/models)には触れない。ranker だけ別途訓練する
  - 使う特徴量・パラメータは src/models/trainer.train_ranker と同じ
  - 保存するのは「予測時点で得られる情報」と「あとから分かる結果」だけ

出力
----
    data/processed/walkforward.db   （SQLite・使い捨て可）
      wf(cutoff, race_date, race_id, stadium, race_no, bet_type,
         combination, model_prob, final_odds, hit)

使い方:
    python scripts/walkforward.py              # 既定の打ち切りで全部
    python scripts/walkforward.py 2026-07-01   # 指定した打ち切りだけ
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import warnings
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lightgbm as lgb                                  # noqa: E402
from src.features.builder import build_features, FEATURE_COLS   # noqa: E402
from src.models import plackett_luce as pl              # noqa: E402
from src.ingestion.database import get_engine, init_db  # noqa: E402
from src.utils.helpers import load_config               # noqa: E402

OUT = Path("data/processed/walkforward.db")
TRAIN_FROM = "2026-01-01"          # 訓練はここから打ち切りまで
HORIZON = 20                       # 各モデルで何日先まで予測するか
CUTOFFS = ["2026-05-01", "2026-05-21", "2026-06-10", "2026-06-30",
           "2026-07-20", "2026-08-09"]
KEEP = 0.742


def train_ranker_at(cutoff: str):
    """打ち切り日までのデータだけで LambdaRank を訓練する（本番には触れない）。"""
    cfg = load_config()
    df = build_features(TRAIN_FROM, cutoff, include_target=True)
    df = df.dropna(subset=["arrival_order"])
    df = df[df["arrival_order"] > 0]
    if df.empty:
        return None, 0
    df = df.sort_values(["race_date", "race_id", "boat_no"]).reset_index(drop=True)
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce").values.astype(float)
    y = (4 - df["arrival_order"].astype(int).clip(1, 4)).clip(0, 3).values
    groups = df.groupby("race_id", sort=False).size().values
    med = np.nanmedian(X, axis=0)
    X = np.where(np.isnan(X), med, X)

    r = lgb.LGBMRanker(
        objective="lambdarank", n_estimators=500, learning_rate=0.05,
        max_depth=6, num_leaves=31, subsample=0.8, colsample_bytree=0.8,
        min_child_samples=20, random_state=cfg["model"].get("random_state", 42),
        n_jobs=-1, verbose=-1, label_gain=[0, 1, 3, 7],
    )
    r.fit(X, y, group=groups)
    r._medians = med
    return r, df["race_id"].nunique()


def predict_window(model, d1: str, d2: str) -> list[tuple]:
    """(d1〜d2) を予測し、確定オッズ・着順と突き合わせて行を返す。"""
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
            "AND o.is_final=1 AND o.odds>0 AND o.bet_type='nirenfuku'"),
            {"d1": d1, "d2": d2}).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no,rr.arrival_order FROM race_results rr "
            "JOIN races r ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order IS NOT NULL"), {"d1": d1, "d2": d2}).fetchall()
        meta = dict(conn.execute(text(
            "SELECT r.id, s.name || '|' || r.race_no || '|' || r.race_date "
            "FROM races r JOIN stadiums s ON s.id=r.stadium_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2"), {"d1": d1, "d2": d2}).fetchall())

    nf = defaultdict(dict)
    for rid, cb, o in od:
        nf[int(rid)][str(cb)] = float(o)
    order = defaultdict(dict)
    for rid, bn, ao in res:
        order[int(rid)][int(ao)] = str(bn)

    rows = []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        o, fin = nf.get(rid, {}), order.get(rid, {})
        if len(o) != 15 or 1 not in fin or 2 not in fin:
            continue
        scores = {int(r.boat_no): float(r.score) for r in grp.itertuples()}
        if len(scores) < 6:
            continue
        top2 = {fin[1], fin[2]}
        exp_s = pl.to_exp_scores(scores, temperature=temp)
        m = meta.get(rid, "||")
        stadium, rno, rdate = (m.split("|") + ["", "", ""])[:3]
        for cb, odds in o.items():
            a, b = sorted(int(x) for x in cb.split("-"))
            rows.append((rdate, rid, stadium, int(rno or 0), "nirenfuku", cb,
                         pl.joint_prob_nirenfuku(exp_s, a, b), odds,
                         1 if set(cb.split("-")) == top2 else 0))
    return rows


def main():
    init_db(load_config())
    cutoffs = sys.argv[1:] or CUTOFFS
    OUT.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(OUT)
    con.execute("""CREATE TABLE IF NOT EXISTS wf(
        cutoff TEXT, race_date TEXT, race_id INT, stadium TEXT, race_no INT,
        bet_type TEXT, combination TEXT, model_prob REAL, final_odds REAL, hit INT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_wf ON wf(cutoff, race_date)")

    for cutoff in cutoffs:
        d1 = str(date.fromisoformat(cutoff) + timedelta(days=1))
        d2 = str(date.fromisoformat(cutoff) + timedelta(days=HORIZON))
        n = con.execute("SELECT COUNT(*) FROM wf WHERE cutoff=?", (cutoff,)).fetchone()[0]
        if n:
            print("%s: 済み (%d行) — 飛ばす" % (cutoff, n))
            continue
        print("%s: 訓練中（%s まで）" % (cutoff, cutoff))
        model, nr = train_ranker_at(cutoff)
        if model is None:
            print("  データなし")
            continue
        print("  訓練 %d レース → 予測 %s〜%s" % (nr, d1, d2))
        rows = predict_window(model, d1, d2)
        # 列は10個（cutoff + predict_window が返す9個）。数を間違えると
        # 訓練をやり直す羽目になるので、テーブル定義と揃っているか数える。
        con.executemany(
            "INSERT INTO wf VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(cutoff,) + r for r in rows])
        con.commit()
        print("  保存 %d行 / %d レース" % (len(rows), len(rows) // 15))

    tot, nr, dmin, dmax = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT race_id), MIN(race_date), MAX(race_date) "
        "FROM wf").fetchone()
    print()
    print("=== 蓄積 ===")
    print("  %d行 / %d レース / %s 〜 %s" % (tot, nr, dmin, dmax))
    print("  （比較: 従来の検証は 758 レース）")


if __name__ == "__main__":
    main()
