"""
予測器 — 学習済みモデルを使って着順確率を計算し DB に保存する。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.builder import FEATURE_COLS, TARGET_COLS, build_features_for_race
from src.models.trainer import load_model
from src.ingestion.database import get_session
from src.ingestion.models import Prediction
from src.utils.logger import get_logger
from src.utils.helpers import load_config

logger = get_logger(__name__)

_MODEL_CACHE: dict[str, object] = {}


def _get_model(target: str):
    if target not in _MODEL_CACHE:
        m = load_model(target)
        if m is None:
            raise RuntimeError(f"モデル未学習: {target} — python main.py train を実行してください")
        _MODEL_CACHE[target] = m
    return _MODEL_CACHE[target]


def predict_race(race_id: int, model_version: str = "v1") -> pd.DataFrame:
    """
    1レース分の着順確率を返す。

    Returns
    -------
    DataFrame: boat_no / win_prob / top2_prob / top3_prob / confidence
    """
    df = build_features_for_race(race_id)
    if df.empty:
        logger.warning(f"予測データなし: race_id={race_id}")
        return pd.DataFrame()

    X = _prepare_X(df)
    results = []
    for i, row in df.iterrows():
        x = X[[i]]
        probs = {}
        for target in TARGET_COLS:
            try:
                model = _get_model(target)
                probs[target] = float(model.predict_proba(x)[0, 1])
            except Exception as e:
                logger.warning(f"予測失敗 {target}: {e}")
                probs[target] = 1 / 6  # 均等確率にフォールバック

        # 信頼度 = 最高確率の艇との差（明確な差があれば高信頼）
        results.append({
            "boat_no": int(row["boat_no"]),
            "win_prob": probs.get("target_win", 1 / 6),
            "top2_prob": probs.get("target_top2", 2 / 6),
            "top3_prob": probs.get("target_top3", 3 / 6),
        })

    pred_df = pd.DataFrame(results)
    # 確率の合計を正規化（win の合計は 1 になるべき）
    total_win = pred_df["win_prob"].sum()
    if total_win > 0:
        pred_df["win_prob"] = pred_df["win_prob"] / total_win
    # 信頼度: win確率の最大値（高いほどモデルが断言している）
    pred_df["confidence"] = pred_df["win_prob"].max()

    return pred_df


def save_predictions(race_id: int, pred_df: pd.DataFrame, model_version: str = "v1") -> None:
    """予測結果を DB に保存する。"""
    with get_session() as session:
        # 既存削除
        session.query(Prediction).filter(
            Prediction.race_id == race_id,
            Prediction.model_version == model_version,
        ).delete()
        for _, row in pred_df.iterrows():
            session.add(Prediction(
                race_id=race_id,
                model_version=model_version,
                boat_no=int(row["boat_no"]),
                win_prob=float(row["win_prob"]),
                top2_prob=float(row["top2_prob"]),
                top3_prob=float(row["top3_prob"]),
                confidence=float(row["confidence"]),
            ))
    logger.debug(f"予測保存: race_id={race_id}, {len(pred_df)} 艇")


def _prepare_X(df: pd.DataFrame) -> np.ndarray:
    X = df[FEATURE_COLS].copy()
    for col in FEATURE_COLS:
        if col not in X.columns:
            X[col] = np.nan
        X[col] = pd.to_numeric(X[col], errors="coerce")
    medians = X.median()
    X = X.fillna(medians)
    return X.values
