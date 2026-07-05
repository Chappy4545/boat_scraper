"""
Plackett-Luce モデルによる順位確率計算

各艇のスコア s_i から順位の joint probability を自己整合的に導出:
- P(艇iが1着) = exp(s_i) / Σ exp(s_j)
- P(艇iが1着, 艇jが2着) = P(i=1st) × exp(s_j) / (Σ exp(s_k) - exp(s_i))
- P(艇iが1着, 艇jが2着, 艇kが3着) = 上記 × exp(s_k) / (Σ exp(s_l) - exp(s_i) - exp(s_j))

これにより:
- sanrentan (順序あり): 直接計算
- sanrenfuku (順序なし): 6順列の和
- nirenfuku (順序なし1-2着): 2順列の和
- nirentan (順序あり1-2着): 直接計算
- tansho (1着): P(艇iが1着)

利点: 独立モデルの掛け算による誤差累積が発生しない、calibration_factor不要。
"""
from __future__ import annotations

from itertools import permutations, combinations
from typing import Sequence

import numpy as np


def scores_to_win_probs(scores: Sequence[float], temperature: float = 1.0) -> dict[int, float]:
    """スコア列から各艇の1着確率を計算 (softmax)。

    scores: dict boat_no -> score
    """
    if isinstance(scores, dict):
        keys = list(scores.keys())
        vals = np.array([float(scores[k]) for k in keys], dtype=float)
    else:
        keys = list(range(1, len(scores) + 1))
        vals = np.array(list(scores), dtype=float)

    # 数値安定性のため最大値でシフト
    vals = vals / max(temperature, 1e-9)
    vals = vals - vals.max()
    exp_vals = np.exp(vals)
    total = exp_vals.sum()
    probs = exp_vals / total if total > 0 else np.ones_like(exp_vals) / len(exp_vals)
    return {k: float(p) for k, p in zip(keys, probs)}


def joint_prob_sanrentan(exp_scores: dict[int, float], a: int, b: int, c: int) -> float:
    """P(a 1着, b 2着, c 3着) を Plackett-Luce で計算。

    exp_scores: 事前に exp を取ったスコア (softmax分母計算のため)
    """
    if a == b or b == c or a == c:
        return 0.0
    total = sum(exp_scores.values())
    remain_1 = total
    remain_2 = total - exp_scores[a]
    remain_3 = total - exp_scores[a] - exp_scores[b]
    if remain_1 <= 0 or remain_2 <= 0 or remain_3 <= 0:
        return 0.0
    p = (exp_scores[a] / remain_1) * (exp_scores[b] / remain_2) * (exp_scores[c] / remain_3)
    return max(0.0, min(1.0, p))


def joint_prob_nirentan(exp_scores: dict[int, float], a: int, b: int) -> float:
    """P(a 1着, b 2着) を Plackett-Luce で計算。"""
    if a == b:
        return 0.0
    total = sum(exp_scores.values())
    remain = total - exp_scores[a]
    if total <= 0 or remain <= 0:
        return 0.0
    p = (exp_scores[a] / total) * (exp_scores[b] / remain)
    return max(0.0, min(1.0, p))


def joint_prob_nirenfuku(exp_scores: dict[int, float], a: int, b: int) -> float:
    """P({a, b} が1-2着) = P(a 1着, b 2着) + P(b 1着, a 2着)。"""
    return joint_prob_nirentan(exp_scores, a, b) + joint_prob_nirentan(exp_scores, b, a)


def joint_prob_sanrenfuku(exp_scores: dict[int, float], a: int, b: int, c: int) -> float:
    """P({a, b, c} が1-3着) = 6順列の和。"""
    return sum(joint_prob_sanrentan(exp_scores, *perm) for perm in permutations([a, b, c]))


def to_exp_scores(scores: dict[int, float], temperature: float = 1.0) -> dict[int, float]:
    """スコア dict → exp(score/T) dict。数値安定性のため最大値でシフト。"""
    keys = list(scores.keys())
    vals = np.array([float(scores[k]) for k in keys], dtype=float) / max(temperature, 1e-9)
    vals = vals - vals.max()
    exp_vals = np.exp(vals)
    return {k: float(e) for k, e in zip(keys, exp_vals)}


def all_bet_probs(scores: dict[int, float], temperature: float = 1.0) -> dict[str, list[dict]]:
    """全 bet_type × 全組合せの確率を計算して返す。

    Returns
    -------
    {
        "tansho":     [{"combination": "1", "model_prob": 0.35}, ...],
        "nirentan":   [{"combination": "1-2", "model_prob": 0.15}, ...],
        "nirenfuku":  [{"combination": "1-2", "model_prob": 0.28}, ...],
        "sanrentan":  [{"combination": "1-2-3", "model_prob": 0.08}, ...],
        "sanrenfuku": [{"combination": "1-2-3", "model_prob": 0.15}, ...],
    }
    """
    exp_scores = to_exp_scores(scores, temperature)
    win_probs = scores_to_win_probs(scores, temperature)
    boats = sorted(scores.keys())

    result = {"tansho": [], "nirentan": [], "nirenfuku": [], "sanrentan": [], "sanrenfuku": []}

    # tansho
    for b in boats:
        result["tansho"].append({
            "combination": str(b),
            "model_prob": float(win_probs[b]),
        })

    # nirentan (順序あり)
    for a, b in permutations(boats, 2):
        result["nirentan"].append({
            "combination": f"{a}-{b}",
            "model_prob": joint_prob_nirentan(exp_scores, a, b),
        })

    # nirenfuku (順序なし)
    for a, b in combinations(boats, 2):
        result["nirenfuku"].append({
            "combination": f"{min(a,b)}-{max(a,b)}",
            "model_prob": joint_prob_nirenfuku(exp_scores, a, b),
        })

    # sanrentan (順序あり)
    for a, b, c in permutations(boats, 3):
        result["sanrentan"].append({
            "combination": f"{a}-{b}-{c}",
            "model_prob": joint_prob_sanrentan(exp_scores, a, b, c),
        })

    # sanrenfuku (順序なし)
    for combo in combinations(boats, 3):
        a, b, c = sorted(combo)
        result["sanrenfuku"].append({
            "combination": f"{a}-{b}-{c}",
            "model_prob": joint_prob_sanrenfuku(exp_scores, a, b, c),
        })

    return result
