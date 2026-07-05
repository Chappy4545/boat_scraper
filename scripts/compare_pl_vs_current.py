"""LambdaRank + Plackett-Luce の効果を過去実績で比較するシミュレーション。

同じレースで:
1. 従来モデル (win/top2/top3 独立 + calibration) が推奨した買い目
2. PL経由で推奨する買い目 (別途 use_ranker=true でシミュレート)
の accuracy / ROI を比較する。

使い方: python scripts/compare_pl_vs_current.py 2026-06-14 2026-06-27
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from itertools import combinations, permutations

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import sqlite3

from src.ingestion.database import init_db, get_engine
from src.models.predictor import predict_race_ranker
from src.models import plackett_luce as pl
from src.utils.helpers import load_config


def load_bets_with_outcome(conn, dates: list[str]) -> pd.DataFrame:
    """指定日の (bet_type, combination, is_hit, odds, model_prob) を返す。"""
    q = """
    SELECT r.race_date, r.id AS race_id, r.stadium_id, r.race_no,
           b.bet_type, b.combination, b.is_hit, b.odds, b.model_prob, b.actual_payout
    FROM bets b JOIN races r ON b.race_id = r.id
    WHERE r.race_date IN ({}) AND b.is_pass = 0
    """.format(",".join([f'"{d}"' for d in dates]))
    return pd.read_sql(q, conn)


def simulate_pl_bets(race_ids: list[int], config: dict) -> pd.DataFrame:
    """PL経由で各レースの全 bet_type × 全組合せの model_prob を計算。"""
    pl_temp = config.get("model", {}).get("pl_temperature", 1.0)
    rows = []
    for rid in race_ids:
        try:
            scores = predict_race_ranker(rid)
            if not scores:
                continue
            all_probs = pl.all_bet_probs(scores, temperature=pl_temp)
            for bet_type, entries in all_probs.items():
                for e in entries:
                    rows.append({
                        "race_id": rid,
                        "bet_type": bet_type,
                        "combination": e["combination"],
                        "pl_model_prob": e["model_prob"],
                    })
        except Exception as e:
            print(f"race_id={rid} PL失敗: {e}")
    return pd.DataFrame(rows)


def main(date_from: str, date_to: str):
    config = load_config()
    init_db(config)

    from datetime import datetime, timedelta
    d1 = datetime.strptime(date_from, "%Y-%m-%d").date()
    d2 = datetime.strptime(date_to, "%Y-%m-%d").date()
    dates = []
    cur = d1
    while cur <= d2:
        dates.append(str(cur))
        cur += timedelta(days=1)

    conn = sqlite3.connect("data/boatrace.db")

    # 従来モデルの実績
    bets = load_bets_with_outcome(conn, dates)
    print(f"\n=== 対象期間: {date_from} 〜 {date_to} ===")
    print(f"従来モデル 実績: {len(bets)}件")

    # 対象レースIDを取得
    race_ids = sorted(bets["race_id"].unique().tolist())
    print(f"対象レース: {len(race_ids)} races")

    # PLで再計算
    print("PL で全組合せの model_prob を計算中...")
    pl_df = simulate_pl_bets(race_ids, config)
    print(f"PL結果: {len(pl_df)} 行")

    # マージ: 従来モデルが買った買い目に PL の model_prob を突合
    merged = bets.merge(
        pl_df, on=["race_id", "bet_type", "combination"], how="left"
    )

    # EV_PL 計算 (calibration_factor 不要)
    merged["ev_pl"] = merged["pl_model_prob"] * merged["odds"]

    # PL 基準 (EV_PL >= 1.2) で買うか判定
    min_ev = config["betting"]["min_expected_value"]
    merged["pl_would_buy"] = merged["ev_pl"] >= min_ev

    # 集計
    print("\n=== 従来 vs PL 比較 ===")
    for bet_type in ["nirenfuku", "sanrenfuku", "sanrentan"]:
        sub = merged[merged["bet_type"] == bet_type]
        if sub.empty:
            continue
        orig_cnt = len(sub)
        orig_hits = int(sub["is_hit"].fillna(0).sum())
        orig_pay = float(sub["actual_payout"].fillna(0).sum())
        orig_roi = orig_pay / (orig_cnt * 100) * 100 if orig_cnt else 0

        pl_sub = sub[sub["pl_would_buy"]]
        pl_cnt = len(pl_sub)
        pl_hits = int(pl_sub["is_hit"].fillna(0).sum())
        pl_pay = float(pl_sub["actual_payout"].fillna(0).sum())
        pl_roi = pl_pay / (pl_cnt * 100) * 100 if pl_cnt else 0

        print(f"{bet_type}:")
        print(f"  従来: {orig_cnt}件 {orig_hits}的中 ROI={orig_roi:.1f}%  損益={orig_pay - orig_cnt * 100:+,.0f}")
        print(f"  PL:   {pl_cnt}件 {pl_hits}的中 ROI={pl_roi:.1f}%  損益={pl_pay - pl_cnt * 100:+,.0f}")

    # 全体
    orig_cnt = len(merged)
    orig_pay = float(merged["actual_payout"].fillna(0).sum())
    pl_sub = merged[merged["pl_would_buy"]]
    pl_cnt = len(pl_sub)
    pl_pay = float(pl_sub["actual_payout"].fillna(0).sum())
    print(f"\n合計:")
    print(f"  従来: {orig_cnt}件  損益={orig_pay - orig_cnt * 100:+,.0f}")
    print(f"  PL:   {pl_cnt}件  損益={pl_pay - pl_cnt * 100:+,.0f}")

    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python scripts/compare_pl_vs_current.py DATE_FROM DATE_TO")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
