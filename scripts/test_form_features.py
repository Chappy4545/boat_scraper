"""自前の結果データから「調子」の特徴量を作り、効果を測る。

現在の30特徴量は全て出走表の印刷値＝通算成績（半年〜1年の平均）。
一方こちらは 200,718 行の結果を持っているのに一切使っていない。
市場（他の予想者）は直近の調子を見ており、モデルが市場に負けている
（対数損失 0.197 vs 0.194）一因はここにあると考えられる。

作る特徴量（すべて「そのレースより前」の情報のみ。未来を見ない）:
  form_win10    直近10走の1着率
  form_top3_10  直近10走の3着内率
  form_avg_rank 直近10走の平均着順
  venue_top3    その場での3着内率（過去全て）
  boat_top3     その艇番での3着内率（過去全て）
  motor_form    そのモーターの直近成績（3着内率）
  days_since    前走からの経過日数（間隔が空くと調子が読めない）

使い方: python scripts/test_form_features.py <train_to> <test_from> <test_to>
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features.builder import build_features, FEATURE_COLS
from src.ingestion.database import get_engine

FORM_COLS = ["form_win10", "form_top3_10", "form_avg_rank",
             "venue_top3", "boat_no_top3", "motor_form", "days_since"]


def build_form(engine) -> pd.DataFrame:
    """レースごと・選手ごとの「そのレース時点での直近成績」を作る。

    自分自身の結果を含めないよう、必ず1行ずらして集計する（未来情報の遮断）。
    """
    sql = """
        SELECT r.race_date, r.id AS race_id, s.code AS stadium_code,
               rr.racer_no, rr.boat_no, rr.arrival_order,
               e.motor_no
        FROM race_results rr
        JOIN races r ON r.id = rr.race_id
        JOIN stadiums s ON s.id = r.stadium_id
        LEFT JOIN race_entries e ON e.race_id = rr.race_id AND e.boat_no = rr.boat_no
        WHERE rr.racer_no IS NOT NULL AND rr.arrival_order IS NOT NULL
    """
    df = pd.read_sql(sql, engine)
    df["race_date"] = pd.to_datetime(df["race_date"])
    df = df.sort_values(["racer_no", "race_date", "race_id"]).reset_index(drop=True)
    df["is_win"] = (df["arrival_order"] == 1).astype(float)
    df["is_top3"] = (df["arrival_order"] <= 3).astype(float)

    g = df.groupby("racer_no", sort=False)
    # shift(1) で自分の結果を除外してから移動平均を取る
    df["form_win10"] = g["is_win"].transform(lambda s: s.shift(1).rolling(10, min_periods=3).mean())
    df["form_top3_10"] = g["is_top3"].transform(lambda s: s.shift(1).rolling(10, min_periods=3).mean())
    df["form_avg_rank"] = g["arrival_order"].transform(lambda s: s.shift(1).rolling(10, min_periods=3).mean())
    df["days_since"] = g["race_date"].transform(lambda s: (s - s.shift(1)).dt.days)

    # 場別・艇番別は「その時点までの通算」（expanding）
    df["venue_top3"] = (df.groupby(["racer_no", "stadium_code"], sort=False)["is_top3"]
                          .transform(lambda s: s.shift(1).expanding(min_periods=3).mean()))
    df["boat_no_top3"] = (df.groupby(["racer_no", "boat_no"], sort=False)["is_top3"]
                            .transform(lambda s: s.shift(1).expanding(min_periods=3).mean()))

    # モーターの直近成績（場×モーター番号で識別）
    df = df.sort_values(["stadium_code", "motor_no", "race_date", "race_id"])
    df["motor_form"] = (df.groupby(["stadium_code", "motor_no"], sort=False)["is_top3"]
                          .transform(lambda s: s.shift(1).rolling(20, min_periods=5).mean()))

    return df[["race_id", "boat_no"] + FORM_COLS]


def evaluate(tr, te, cols, label):
    import lightgbm as lgb
    from src.models import plackett_luce as pl

    tr = tr.sort_values("race_id")
    Xtr = tr[cols].apply(pd.to_numeric, errors="coerce")
    med = Xtr.median()
    ytr = (6 - tr["_rank"]).clip(lower=0).astype(int)
    grp = tr.groupby("race_id", sort=False).size().values
    m = lgb.LGBMRanker(objective="lambdarank", n_estimators=300, learning_rate=0.05,
                       num_leaves=31, min_child_samples=30, random_state=42, verbose=-1)
    m.fit(Xtr.fillna(med), ytr, group=grp)

    te = te.sort_values("race_id")
    Xte = te[cols].apply(pd.to_numeric, errors="coerce").fillna(med)
    te = te.assign(_s=m.predict(Xte))

    top = te.loc[te.groupby("race_id")["_s"].idxmax()]
    acc = (top["_rank"] == 1).mean()

    ll, hit, tot = [], 0, 0
    for rid, g in te.groupby("race_id", sort=False):
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_s"])}
        if len(scores) < 6:
            continue
        actual = set(g.loc[g["_rank"] <= 2, "boat_no"].astype(int))
        if len(actual) != 2:
            continue
        key = f"{min(actual)}-{max(actual)}"
        for c in pl.all_bet_probs(scores, temperature=1.0).get("nirenfuku", []):
            p = float(c["model_prob"]); y = 1 if c["combination"] == key else 0
            ll.append(-(y * np.log(max(p, 1e-9)) + (1 - y) * np.log(max(1 - p, 1e-9))))
            if p >= 0.30:
                tot += 1; hit += y
    print(f"  {label:<26}特徴量{len(cols):>3}個  1着 {acc*100:5.2f}%  "
          f"2連複30%帯 {(hit/tot*100 if tot else 0):5.2f}%({tot})  対数損失 {np.mean(ll):.5f}")
    return acc, (hit / tot if tot else 0), float(np.mean(ll))


def main():
    train_to, test_from, test_to = sys.argv[1:4]
    engine = get_engine()

    print("結果データから調子の特徴量を構築中…")
    form = build_form(engine)
    print(f"  {len(form):,} 行")

    df = build_features(None, test_to, include_target=True).dropna(subset=["target_win"])
    df["_rank"] = np.where(df["target_win"] == 1, 1,
                    np.where(df["target_top2"] == 1, 2,
                      np.where(df["target_top3"] == 1, 3, 4)))
    df = df.merge(form, on=["race_id", "boat_no"], how="left")
    cover = df[FORM_COLS].notna().mean()
    print("  カバー率:", {c: f"{v*100:.0f}%" for c, v in cover.items()})

    tr = df[df["race_date"].astype(str) <= train_to]
    te = df[df["race_date"].astype(str) >= test_from]
    print(f"\n訓練 {tr['race_id'].nunique():,} レース / 検証 {te['race_id'].nunique():,} レース\n")

    base = [c for c in FEATURE_COLS if c in df.columns]
    a1, r1, l1 = evaluate(tr, te, base, "現行30特徴量")
    a2, r2, l2 = evaluate(tr, te, base + FORM_COLS, "＋調子（7項目）")
    print(f"\n  1着的中率  {(a2-a1)*100:+.2f}pt")
    print(f"  2連複30%帯 {(r2-r1)*100:+.2f}pt")
    print(f"  対数損失   {l2-l1:+.5f}（マイナスなら改善／市場は0.19410）")


if __name__ == "__main__":
    main()


def importance():
    """どの特徴量が効いているかを見る（採用を絞るため）。"""
    import lightgbm as lgb
    engine = get_engine()
    form = build_form(engine)
    df = build_features(None, "2026-08-11", include_target=True).dropna(subset=["target_win"])
    df["_rank"] = np.where(df["target_win"] == 1, 1,
                    np.where(df["target_top2"] == 1, 2,
                      np.where(df["target_top3"] == 1, 3, 4)))
    df = df.merge(form, on=["race_id", "boat_no"], how="left")
    tr = df[df["race_date"].astype(str) <= "2026-06-30"].sort_values("race_id")
    cols = [c for c in FEATURE_COLS if c in df.columns] + FORM_COLS
    X = tr[cols].apply(pd.to_numeric, errors="coerce")
    m = lgb.LGBMRanker(objective="lambdarank", n_estimators=300, learning_rate=0.05,
                       num_leaves=31, min_child_samples=30, random_state=42, verbose=-1)
    m.fit(X.fillna(X.median()), (6 - tr["_rank"]).clip(lower=0).astype(int),
          group=tr.groupby("race_id", sort=False).size().values)
    imp = pd.Series(m.feature_importances_, index=cols).sort_values(ascending=False)
    print("\n=== 特徴量の寄与（上位15）===")
    for k, v in imp.head(15).items():
        mark = "  ← 新規" if k in FORM_COLS else ""
        print(f"  {k:<22}{int(v):>6}{mark}")
    print("\n=== 新規特徴量の順位 ===")
    for c in FORM_COLS:
        print(f"  {c:<22}{int(imp[c]):>6}  （{list(imp.index).index(c)+1}位/{len(cols)}）")
