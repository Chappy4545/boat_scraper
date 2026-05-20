"""
バックテスト — 指南書 Step 8

・時系列分割（ランダム分割禁止）
・未来情報の漏洩なし
・全体回収率・的中率・最大DD・月別/場別/EV帯別 集計
"""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd

from src.features.builder import build_features, FEATURE_COLS, TARGET_COLS
from src.models.trainer import load_model
from src.betting.ev_calculator import generate_bets
from src.betting.money_manager import MoneyManager, BankrollState
from src.ingestion.database import get_session, get_engine
from src.ingestion.models import BacktestResult, Odds
from src.utils.helpers import load_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────
# 公開 API
# ──────────────────────────────────────────────

def run_backtest(
    date_from: str,
    date_to: str,
    model_version: str = "v1",
    config: dict | None = None,
) -> dict:
    """
    時系列バックテストを実行して結果を DB に保存する。

    Parameters
    ----------
    date_from / date_to : 'YYYY-MM-DD'
    """
    if config is None:
        config = load_config()

    logger.info(f"バックテスト開始: {date_from} 〜 {date_to}")

    # 全データ取得
    df = build_features(date_from, date_to, include_target=True)
    if df.empty:
        logger.error("バックテスト: データなし")
        return {}

    # 目的変数が揃っているレースのみ
    df = df.dropna(subset=["target_win"])

    # 学習期間 / テスト期間を 60/40 で分割（時系列）
    df = df.sort_values("race_date")
    split_idx = int(len(df) * 0.6)
    train_df = df.iloc[:split_idx]
    test_df = df.iloc[split_idx:]

    logger.info(f"学習: {len(train_df)} 行 / テスト: {len(test_df)} 行")

    # モデル読み込み（学習済みを前提）
    models = {t: load_model(t) for t in TARGET_COLS}
    if any(m is None for m in models.values()):
        logger.error("未学習モデルあり — python main.py train を先に実行")
        return {}

    # テスト期間でシミュレーション
    mm = MoneyManager(config)
    state = mm.new_state()
    engine = get_engine()

    records = []
    race_groups = test_df.groupby("race_id")

    for race_id, race_df in race_groups:
        if state.check_stop(config):
            logger.warning(f"停止条件到達: {state.stop_reason}")
            break

        # 予測
        pred_df = _predict_from_df(race_df, models)
        if pred_df.empty:
            continue

        # オッズ取得
        odds_df = _load_odds(engine, int(race_id))

        # 買い目生成
        bets_df = generate_bets(pred_df, odds_df, config, model_version)

        # 実際の結果
        actual_top3 = _get_actual_result(race_df)

        for _, bet in bets_df.iterrows():
            if bet["is_pass"]:
                records.append(_pass_record(race_df, bet))
                continue

            amount = mm.calc_bet_amount(
                float(bet["expected_value"]),
                float(bet["model_prob"]),
                float(bet["odds"]),
                state,
            )
            if amount == 0:
                continue

            is_hit, payout = _check_hit(bet, actual_top3, amount)
            state.update_after_bet(amount, payout)
            state.check_stop(config)

            records.append({
                "race_id": race_id,
                "race_date": race_df["race_date"].iloc[0],
                "stadium_code": race_df["stadium_code"].iloc[0],
                "bet_type": bet["bet_type"],
                "combination": bet["combination"],
                "model_prob": bet["model_prob"],
                "odds": bet["odds"],
                "expected_value": bet["expected_value"],
                "amount": amount,
                "is_hit": is_hit,
                "payout": payout,
                "bankroll": state.bankroll,
                "is_pass": False,
                "pass_reason": "",
            })

    rec_df = pd.DataFrame(records)
    summary = _summarize(rec_df, state, date_from, date_to, model_version)
    _save_result(summary, model_version, date_from, date_to)
    return summary


# ──────────────────────────────────────────────
# 内部実装
# ──────────────────────────────────────────────

def _predict_from_df(race_df: pd.DataFrame, models: dict) -> pd.DataFrame:
    import numpy as np
    X = race_df[FEATURE_COLS].copy()
    for col in FEATURE_COLS:
        if col not in X.columns:
            X[col] = np.nan
        X[col] = pd.to_numeric(X[col], errors="coerce")
    medians = X.median().fillna(0)
    X = X.fillna(medians).values

    results = []
    for i, (_, row) in enumerate(race_df.iterrows()):
        probs = {}
        for target in TARGET_COLS:
            try:
                probs[target] = float(models[target].predict_proba(X[[i]])[0, 1])
            except Exception:
                probs[target] = 1 / 6
        results.append({
            "boat_no": int(row["boat_no"]),
            "win_prob": probs.get("target_win", 1 / 6),
            "top2_prob": probs.get("target_top2", 2 / 6),
            "top3_prob": probs.get("target_top3", 3 / 6),
        })

    pred_df = pd.DataFrame(results)
    total_win = pred_df["win_prob"].sum()
    if total_win > 0:
        pred_df["win_prob"] /= total_win
    total_top2 = pred_df["top2_prob"].sum()
    if total_top2 > 0:
        pred_df["top2_prob"] = pred_df["top2_prob"] / total_top2 * 2.0
    total_top3 = pred_df["top3_prob"].sum()
    if total_top3 > 0:
        pred_df["top3_prob"] = pred_df["top3_prob"] / total_top3 * 3.0
    pred_df["top2_prob"] = np.maximum(pred_df["top2_prob"], pred_df["win_prob"])
    pred_df["top3_prob"] = np.maximum(pred_df["top3_prob"], pred_df["top2_prob"])
    pred_df["confidence"] = pred_df["win_prob"].max()
    return pred_df


def _load_odds(engine, race_id: int) -> pd.DataFrame:
    from sqlalchemy import text
    sql = "SELECT bet_type, combination, odds FROM odds WHERE race_id = :rid AND is_final = 1"
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params={"rid": race_id})
        if not df.empty:
            return df
        # Fall back to payouts table (payout yen / 100 = odds multiplier)
        pay_sql = """SELECT bet_type, combination, CAST(payout AS FLOAT) / 100.0 AS odds
                     FROM payouts WHERE race_id = :rid
                     AND bet_type IN ('sanrentan','sanrenfuku','nirentan','nirenfuku')"""
        return pd.read_sql(text(pay_sql), conn, params={"rid": race_id})


def _get_actual_result(race_df: pd.DataFrame) -> list[int]:
    """着順順に枠番のリストを返す（最大3着まで）。"""
    if "target_win" not in race_df.columns:
        return []
    # race_df は艇単位なので、target_win=1 の boat_no, target_top2=1 かつ win!=1 の boat_no...
    order = []
    for place, col in [(1, "target_win"), (2, "target_top2"), (3, "target_top3")]:
        sub = race_df[race_df[col] == 1] if col in race_df.columns else pd.DataFrame()
        for _, r in sub.iterrows():
            if int(r["boat_no"]) not in order:
                order.append(int(r["boat_no"]))
    return order[:3]


def _check_hit(bet, actual_top3: list[int], amount: int) -> tuple[bool, int]:
    """的中判定と払戻計算。"""
    combo = [int(x) for x in bet["combination"].split("-")]
    bet_type = bet["bet_type"]
    if not actual_top3 or len(actual_top3) < 2:
        return False, 0

    if bet_type == "nirentan":
        hit = len(combo) == 2 and combo[0] == actual_top3[0] and combo[1] == actual_top3[1]
    elif bet_type == "nirenfuku":
        hit = set(combo[:2]) == set(actual_top3[:2])
    elif bet_type == "sanrentan":
        hit = len(combo) == 3 and combo == actual_top3[:3]
    elif bet_type == "sanrenfuku":
        hit = set(combo) == set(actual_top3[:3])
    else:
        hit = False

    if hit:
        payout = int(amount * float(bet["odds"]))
        return True, payout
    return False, 0


def _pass_record(race_df: pd.DataFrame, bet) -> dict:
    return {
        "race_id": race_df["race_id"].iloc[0] if "race_id" in race_df else None,
        "race_date": race_df["race_date"].iloc[0],
        "stadium_code": race_df["stadium_code"].iloc[0],
        "bet_type": "", "combination": "",
        "model_prob": None, "odds": None, "expected_value": None,
        "amount": 0, "is_hit": None, "payout": 0,
        "bankroll": None, "is_pass": True, "pass_reason": bet.get("pass_reason", ""),
    }


def _summarize(rec_df: pd.DataFrame, state: BankrollState,
               date_from: str, date_to: str, model_version: str) -> dict:
    if rec_df.empty:
        return {}

    buy = rec_df[~rec_df["is_pass"]]
    pass_ = rec_df[rec_df["is_pass"]]

    total_inv = int(buy["amount"].sum())
    total_ret = int(buy["payout"].sum())
    hits = int(buy["is_hit"].sum()) if "is_hit" in buy.columns else 0
    n_bets = len(buy)
    roi = total_ret / total_inv if total_inv > 0 else 0.0

    # 最大ドローダウン
    if "bankroll" in buy.columns and not buy["bankroll"].isna().all():
        bankroll_series = buy["bankroll"].dropna().values
        peak = np.maximum.accumulate(bankroll_series)
        dd = (peak - bankroll_series) / np.where(peak == 0, 1, peak)
        max_dd = float(dd.max()) if len(dd) > 0 else 0.0
    else:
        max_dd = float(state.drawdown)

    # 連敗
    if "is_hit" in buy.columns:
        max_consec = _max_consecutive_losses(buy["is_hit"].tolist())
    else:
        max_consec = state.consecutive_losses

    # 月別
    monthly = {}
    if "race_date" in buy.columns:
        buy = buy.copy()
        buy["month"] = pd.to_datetime(buy["race_date"]).dt.strftime("%Y-%m")
        for m, g in buy.groupby("month"):
            inv = int(g["amount"].sum())
            ret = int(g["payout"].sum())
            monthly[m] = {"investment": inv, "return": ret, "roi": ret / inv if inv else 0}

    # 場別
    by_stadium = {}
    if "stadium_code" in buy.columns:
        for sc, g in buy.groupby("stadium_code"):
            inv = int(g["amount"].sum())
            ret = int(g["payout"].sum())
            by_stadium[sc] = {"investment": inv, "return": ret, "roi": ret / inv if inv else 0}

    # EV帯別
    ev_bands = {}
    if "expected_value" in buy.columns:
        buy["ev_band"] = pd.cut(buy["expected_value"], bins=[0, 1.1, 1.2, 1.5, 2.0, 99],
                                labels=["<1.1", "1.1-1.2", "1.2-1.5", "1.5-2.0", ">2.0"])
        for band, g in buy.groupby("ev_band", observed=True):
            inv = int(g["amount"].sum())
            ret = int(g["payout"].sum())
            ev_bands[str(band)] = {"investment": inv, "return": ret, "roi": ret / inv if inv else 0}

    summary = {
        "model_version": model_version,
        "date_from": date_from,
        "date_to": date_to,
        "total_races": int(rec_df["race_id"].nunique()) if "race_id" in rec_df.columns else 0,
        "bet_races": int(buy["race_id"].nunique()) if "race_id" in buy.columns else 0,
        "pass_races": int(pass_["race_id"].nunique()) if "race_id" in pass_.columns else 0,
        "total_bets": n_bets,
        "hits": hits,
        "hit_rate": hits / n_bets if n_bets > 0 else 0.0,
        "total_investment": total_inv,
        "total_return": total_ret,
        "roi": roi,
        "max_drawdown": max_dd,
        "max_consecutive_losses": max_consec,
        "avg_odds": float(buy["odds"].mean()) if "odds" in buy.columns and n_bets > 0 else 0.0,
        "monthly": monthly,
        "by_stadium": by_stadium,
        "ev_bands": ev_bands,
    }

    logger.info(
        f"バックテスト結果: 回収率={roi*100:.1f}% 的中率={summary['hit_rate']*100:.1f}% "
        f"最大DD={max_dd*100:.1f}% 購入レース={summary['bet_races']}"
    )
    return summary


def _max_consecutive_losses(hits: list) -> int:
    max_c = cur = 0
    for h in hits:
        if not h:
            cur += 1
            max_c = max(max_c, cur)
        else:
            cur = 0
    return max_c


def _save_result(summary: dict, model_version: str, date_from: str, date_to: str) -> None:
    from datetime import date as date_cls
    with get_session() as session:
        result = BacktestResult(
            model_version=model_version,
            date_start=date_cls.fromisoformat(date_from),
            date_end=date_cls.fromisoformat(date_to),
            total_races=summary.get("total_races"),
            bet_races=summary.get("bet_races"),
            pass_races=summary.get("pass_races"),
            total_bets=summary.get("total_bets"),
            hits=summary.get("hits"),
            hit_rate=summary.get("hit_rate"),
            total_investment=summary.get("total_investment"),
            total_return=summary.get("total_return"),
            roi=summary.get("roi"),
            max_drawdown=summary.get("max_drawdown"),
            max_consecutive_losses=summary.get("max_consecutive_losses"),
            avg_odds=summary.get("avg_odds"),
            summary_json=json.dumps(summary, ensure_ascii=False),
        )
        session.add(result)
    logger.info("バックテスト結果をDBに保存しました")
