"""
期待値計算と買い目生成 — 指南書 Step 6

期待値 = モデル推定的中確率 × オッズ

買い条件:
  - 期待値 >= min_expected_value (デフォルト 1.10)
  - モデル信頼度 >= min_model_confidence
  - min_odds <= オッズ <= max_odds
  - 1レースあたりの買い目数 <= max_bets_per_race
  - 条件を満たさないレースは「見送り」
"""
from __future__ import annotations

from itertools import permutations, combinations
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────
# 公開 API
# ──────────────────────────────────────────────

def generate_bets(
    pred_df: pd.DataFrame,
    odds_df: pd.DataFrame,
    config: dict,
    model_version: str = "v1",
) -> pd.DataFrame:
    """
    予測確率とオッズから推奨買い目を生成する。

    Parameters
    ----------
    pred_df  : boat_no / win_prob / top2_prob / top3_prob / confidence
    odds_df  : bet_type / combination / odds

    Returns
    -------
    DataFrame: bet_type / combination / model_prob / odds / expected_value /
               recommended_amount / is_pass / pass_reason
    """
    cfg = config["betting"]
    min_ev = cfg["min_expected_value"]
    min_conf = cfg["min_model_confidence"]
    min_odds = cfg["min_odds"]
    max_odds = cfg["max_odds"]
    max_bets = cfg["max_bets_per_race"]
    bet_types = cfg.get("bet_types", ["2連複", "2連単", "3連複", "3連単"])

    if pred_df.empty:
        return _pass_df("予測データなし")

    confidence = float(pred_df["confidence"].max())
    if confidence < min_conf:
        return _pass_df(f"モデル信頼度不足 ({confidence:.3f} < {min_conf})")

    # 各買い式の推定確率を計算
    candidates = []
    for bet_type in bet_types:
        rows = _calc_bet_probs(pred_df, bet_type)
        candidates.extend(rows)

    if not candidates:
        return _pass_df("買い目候補なし")

    cands_df = pd.DataFrame(candidates)

    # オッズをマージ
    cands_df = _merge_odds(cands_df, odds_df)

    # 期待値計算
    cands_df["expected_value"] = cands_df["model_prob"] * cands_df["odds"]

    # フィルタリング
    cands_df["is_pass"] = False
    cands_df["pass_reason"] = ""

    mask_ev = cands_df["expected_value"] < min_ev
    mask_odds_low = cands_df["odds"] < min_odds
    mask_odds_high = cands_df["odds"] > max_odds
    mask_no_odds = cands_df["odds"].isna()

    cands_df.loc[mask_ev, ["is_pass", "pass_reason"]] = [True, f"EV < {min_ev}"]
    cands_df.loc[mask_odds_low, ["is_pass", "pass_reason"]] = [True, f"オッズ低({min_odds}未満)"]
    cands_df.loc[mask_odds_high, ["is_pass", "pass_reason"]] = [True, f"大穴除外({max_odds}超)"]
    cands_df.loc[mask_no_odds, ["is_pass", "pass_reason"]] = [True, "オッズなし"]

    # 買い目を EV 降順でソート、上限本数まで
    buy = cands_df[~cands_df["is_pass"]].sort_values("expected_value", ascending=False)
    if len(buy) > max_bets:
        # 上位 max_bets のみ採用、残りは見送り
        buy_top = buy.head(max_bets).index
        cands_df.loc[~cands_df.index.isin(buy_top) & ~cands_df["is_pass"], ["is_pass", "pass_reason"]] = \
            [True, f"買い目数上限({max_bets}本)超過"]

    if cands_df[~cands_df["is_pass"]].empty:
        return _pass_df("全買い目が見送り条件に該当")

    cands_df["model_version"] = model_version
    return cands_df


# ──────────────────────────────────────────────
# 買い式別 推定確率計算
# ──────────────────────────────────────────────

def _calc_bet_probs(pred: pd.DataFrame, bet_type: str) -> list[dict]:
    """各買い式の全組み合わせに対して推定的中確率を返す。"""
    boats = pred.set_index("boat_no").to_dict("index")
    result = []

    if bet_type == "2連単":  # 1着-2着（順番あり）
        for a, b in permutations(boats.keys(), 2):
            p = boats[a]["win_prob"] * boats[b]["top2_prob"]
            # a が1着の場合に b が2着以内になる確率の近似
            p = boats[a]["win_prob"] * (boats[b]["top2_prob"] - boats[b]["win_prob"]) / max(1 - boats[a]["win_prob"], 1e-6)
            p = max(0, p)
            result.append({"bet_type": "nirentan", "combination": f"{a}-{b}", "model_prob": p})

    elif bet_type == "2連複":  # 1-2着（順番なし）
        for a, b in combinations(boats.keys(), 2):
            p = _prob_top2_fuku(boats, a, b)
            result.append({"bet_type": "nirenfuku", "combination": f"{min(a,b)}-{max(a,b)}", "model_prob": p})

    elif bet_type == "3連単":  # 1-2-3着（順番あり）
        for a, b, c in permutations(boats.keys(), 3):
            p = _prob_sanrentan(boats, a, b, c)
            result.append({"bet_type": "sanrentan", "combination": f"{a}-{b}-{c}", "model_prob": p})

    elif bet_type == "3連複":  # 1-2-3着（順番なし）
        for combo in combinations(boats.keys(), 3):
            a, b, c = sorted(combo)
            p = sum(
                _prob_sanrentan(boats, *perm)
                for perm in permutations(combo)
            )
            result.append({"bet_type": "sanrenfuku", "combination": f"{a}-{b}-{c}", "model_prob": p})

    return result


def _prob_top2_fuku(boats: dict, a: int, b: int) -> float:
    """艇 a と b が1-2着を占める確率の近似。"""
    p_a1_b2 = boats[a]["win_prob"] * max(boats[b]["top2_prob"] - boats[b]["win_prob"], 0) / max(1 - boats[a]["win_prob"], 1e-9)
    p_b1_a2 = boats[b]["win_prob"] * max(boats[a]["top2_prob"] - boats[a]["win_prob"], 0) / max(1 - boats[b]["win_prob"], 1e-9)
    return max(0, p_a1_b2 + p_b1_a2)


def _prob_sanrentan(boats: dict, a: int, b: int, c: int) -> float:
    """艇 a 1着 → b 2着 → c 3着の確率近似。"""
    p_a = boats[a]["win_prob"]
    rem_b = max(1 - p_a, 1e-9)
    p_b_given_a = max(boats[b]["top2_prob"] - boats[b]["win_prob"], 0) / rem_b
    rem_c = max(1 - p_a - boats[b]["win_prob"], 1e-9)
    p_c_given_ab = max(boats[c]["top3_prob"] - boats[c]["top2_prob"], 0) / rem_c
    return max(0, p_a * p_b_given_a * p_c_given_ab)


def _merge_odds(df: pd.DataFrame, odds_df: pd.DataFrame) -> pd.DataFrame:
    if odds_df.empty:
        df["odds"] = np.nan
        return df
    type_map = {"nirentan": "nirentan", "nirenfuku": "nirenfuku",
                "sanrentan": "sanrentan", "sanrenfuku": "sanrenfuku"}
    odds_lookup = odds_df[["bet_type", "combination", "odds"]].copy()
    df = df.merge(odds_lookup, on=["bet_type", "combination"], how="left")
    return df


def _pass_df(reason: str) -> pd.DataFrame:
    logger.info(f"見送り: {reason}")
    return pd.DataFrame([{
        "bet_type": "", "combination": "", "model_prob": np.nan,
        "odds": np.nan, "expected_value": np.nan,
        "recommended_amount": 0, "is_pass": True, "pass_reason": reason,
    }])
