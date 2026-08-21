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

    engine = get_engine()

    # 対象レース: odds(is_final=1) と結果が両方揃うレースのみ。
    #   payouts は的中組合せしか持たないため、fallback を使うと
    #   「外れ目はオッズなし→見送り」となり的中目だけ買う未来情報リークになる。
    rows = _load_backtest_race_ids(engine, date_from, date_to)
    if not rows:
        logger.error("バックテスト: 対象レースなし（odds実在レースが0件）")
        return {}
    logger.info(f"対象レース: {len(rows)} 件（odds実在のみ）")

    use_ranker = config.get("model", {}).get("use_ranker", False)
    pl_temperature = float(config.get("model", {}).get("pl_temperature", 1.0))

    # 本番と同一の予測経路を使う（backtest/predict の乖離を排除）
    from src.models.predictor import predict_race, predict_race_pl

    mm = MoneyManager(config)
    state = mm.new_state()

    records = []

    for race_id, race_date, stadium_code in rows:
        if state.check_stop(config):
            logger.warning(f"停止条件到達: {state.stop_reason}")
            break

        try:
            pred_df = predict_race(race_id, model_version)
            if pred_df.empty:
                continue

            odds_df = _load_odds(engine, int(race_id))

            pl_probs = predict_race_pl(race_id, temperature=pl_temperature) if use_ranker else None
            bets_df = generate_bets(pred_df, odds_df, config, model_version, pl_probs=pl_probs)
        except Exception as e:
            logger.warning(f"  race_id={race_id} 予測失敗: {e}")
            continue

        for _, bet in bets_df.iterrows():
            if bet["is_pass"]:
                records.append(_pass_record(race_id, race_date, stadium_code, bet))
                continue

            amount = mm.calc_bet_amount(
                float(bet["expected_value"]),
                float(bet["model_prob"]),
                float(bet["odds"]),
                state,
            )
            if amount == 0:
                continue

            # 的中判定は本番 judge と同一方式（payouts 照合）
            is_hit, payout = _judge_by_payouts(
                engine, int(race_id), str(bet["bet_type"]), str(bet["combination"]), amount
            )
            state.update_after_bet(amount, payout)
            state.check_stop(config)

            records.append({
                "race_id": race_id,
                "race_date": race_date,
                "stadium_code": stadium_code,
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

def _load_backtest_race_ids(engine, date_from: str, date_to: str) -> list[tuple]:
    """バックテスト対象レースを返す。

    odds(is_final=1) と race_results が両方存在するレースに限定する。
    payouts fallback を許すと外れ目のオッズが取れず、
    「的中目だけ買える」未来情報リークが発生するため。
    """
    from sqlalchemy import text
    sql = """
        SELECT DISTINCT rc.id, rc.race_date, rc.stadium_id
        FROM races rc
        JOIN odds o ON o.race_id = rc.id AND o.is_final = 1
        JOIN race_results rr ON rr.race_id = rc.id
        WHERE rc.race_date BETWEEN :d1 AND :d2
        ORDER BY rc.race_date, rc.id
    """
    with engine.connect() as conn:
        rs = conn.execute(text(sql), {"d1": date_from, "d2": date_to})
        return [(r[0], str(r[1]), r[2]) for r in rs]


def _judge_by_payouts(engine, race_id: int, bet_type: str,
                      combination: str, amount: int) -> tuple[bool, int]:
    """的中判定。本番 cmd_judge と同一方式で payouts を照合する。

    payouts に (race_id, bet_type, combination) が存在すれば的中。
    払戻は payout(100円あたり) × amount / 100。
    """
    from sqlalchemy import text
    sql = """SELECT payout FROM payouts
             WHERE race_id = :rid AND bet_type = :bt AND combination = :cb
             LIMIT 1"""
    with engine.connect() as conn:
        row = conn.execute(
            text(sql), {"rid": race_id, "bt": bet_type, "cb": combination}
        ).fetchone()
    if row is None:
        return False, 0
    return True, int(float(row[0]) * amount / 100.0)


def _load_odds(engine, race_id: int) -> pd.DataFrame:
    """確定オッズを返す。オッズが無ければ空を返す（payouts へフォールバックしない）。

    payouts は的中組合せしか持たないため、そこからオッズを作ると
    「外れ目はオッズなし→見送り、的中目だけ購入可能」となり、
    未来情報リークで回収率が実態より大幅に良く見える。
    予測は本来レース前に行うものでありオッズ未取得なら見送るのが正しいので、
    本番・バックテストとも fallback は許可しない。

    2026-08-21 追記: 当日の板は is_final=0 に入るようになった（saver.save_odds）。
    買えるのは板の方なので、板があればそれを使う。無い場合だけ確定オッズに
    落ちる（過去日の再予測など）。どちらを使ったかは呼び出し側で分かるよう
    列 is_live を返す。
    """
    from sqlalchemy import text
    sql = ("SELECT bet_type, combination, odds, is_live FROM odds "
           "WHERE race_id = :rid AND is_final = :fin AND odds > 0")
    with engine.connect() as conn:
        live = pd.read_sql(text(sql), conn, params={"rid": race_id, "fin": 0})
        if not live.empty:
            return live
        return pd.read_sql(text(sql), conn, params={"rid": race_id, "fin": 1})


def _pass_record(race_id, race_date, stadium_code, bet) -> dict:
    return {
        "race_id": race_id,
        "race_date": race_date,
        "stadium_code": stadium_code,
        "bet_type": "", "combination": "",
        "model_prob": None, "odds": None, "expected_value": None,
        "amount": 0, "is_hit": None, "payout": 0,
        "bankroll": None, "is_pass": True,
        "pass_reason": bet.get("pass_reason", "") if hasattr(bet, "get") else "",
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
