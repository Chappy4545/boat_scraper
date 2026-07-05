"""
モデル学習 — 指南書 Step 4-5

・LogisticRegression / RandomForest / LightGBM / CatBoost を比較
・キャリブレーション（Platt scaling / Isotonic regression）を適用
・時系列分割で CV を実施（ランダム分割禁止）
・モデルを joblib で保存
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import joblib

from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.metrics import log_loss, brier_score_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

import lightgbm as lgb
from catboost import CatBoostClassifier

from src.features.builder import FEATURE_COLS, TARGET_COLS
from src.utils.logger import get_logger
from src.utils.helpers import load_config

logger = get_logger(__name__)

MODEL_DIR = Path("data/processed/models")


# ──────────────────────────────────────────────
# 公開 API
# ──────────────────────────────────────────────

def train_all(df: pd.DataFrame, config: dict | None = None) -> dict:
    """
    全目的変数 × 全モデルを学習して評価結果を返す。
    最良モデルを MODEL_DIR に保存する。
    """
    if config is None:
        config = load_config()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    df_clean = _prepare(df)
    if df_clean.empty:
        logger.error("学習データなし")
        return {}

    results = {}
    best_models: dict[str, object] = {}

    for target in TARGET_COLS:
        if target not in df_clean.columns or df_clean[target].isna().all():
            logger.warning(f"目的変数 {target} が存在しないためスキップ")
            continue

        mask = df_clean[target].notna()
        X = df_clean.loc[mask, FEATURE_COLS].values
        y = df_clean.loc[mask, target].values.astype(int)
        dates = pd.to_datetime(df_clean.loc[mask, "race_date"]).values

        logger.info(f"=== {target}: {len(y)} 件, 正例率 {y.mean():.3f} ===")

        target_results, best_model, best_name = _train_target(X, y, dates, target, config)
        results[target] = target_results
        best_models[target] = best_model

        model_path = MODEL_DIR / f"{target}_{best_name}.joblib"
        joblib.dump(best_model, model_path)
        logger.info(f"保存: {model_path}")

    # 評価サマリを JSON 保存
    summary_path = MODEL_DIR / f"training_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"評価サマリ: {summary_path}")

    return results


def load_model(target: str) -> object | None:
    """最新の保存済みモデルを読み込む（mtime 最新のものを選ぶ）。"""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    candidates = list(MODEL_DIR.glob(f"{target}_*.joblib"))
    if not candidates:
        return None
    # 異なる model 名 (logreg/lightgbm/...) のファイルが残っているとき、
    # alphabetical sort では古いモデルが選ばれることがあるため mtime 順を採用
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return joblib.load(candidates[-1])


def load_ranker() -> object | None:
    """LambdaRankモデルを読み込む。"""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    candidates = list(MODEL_DIR.glob("ranker_*.joblib"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return joblib.load(candidates[-1])


def train_ranker(df: pd.DataFrame, config: dict | None = None) -> dict:
    """LambdaRank モデルを学習する (Plackett-Luce用の強さスコア学習)。

    - group: race_id (各レース=6艇)
    - label: relevance = 4 - min(3, arrival_order-1) つまり
             1着=3, 2着=2, 3着=1, 4-6着=0
    - 目的: 各艇の相対的な強さスコアを学習
    """
    if config is None:
        config = load_config()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    df_clean = _prepare(df)
    if df_clean.empty or "arrival_order" not in df_clean.columns:
        logger.error("ranker学習: arrival_orderが必要")
        return {}

    mask = df_clean["arrival_order"].notna() & df_clean["arrival_order"] > 0
    df_c = df_clean.loc[mask].copy()
    if df_c.empty:
        logger.error("ranker学習: 有効データなし")
        return {}

    # relevance: 1着=3, 2着=2, 3着=1, 4-6着=0
    df_c["_rel"] = (4 - df_c["arrival_order"].astype(int).clip(1, 4)).clip(0, 3)

    # race_id順にソート (group 分割の前提)
    df_c = df_c.sort_values(["race_date", "race_id", "boat_no"]).reset_index(drop=True)
    X = df_c[FEATURE_COLS].values
    y = df_c["_rel"].astype(int).values
    groups = df_c.groupby("race_id", sort=False).size().values  # 各グループのサイズ
    dates = pd.to_datetime(df_c["race_date"]).values

    logger.info(f"=== ranker: {len(y)} 行, {len(groups)} レース ===")

    # 欠損補完 (訓練前の中央値)
    medians = np.nanmedian(X, axis=0)
    X_filled = np.where(np.isnan(X), medians, X)

    # LightGBM Ranker
    rs = config["model"].get("random_state", 42)
    ranker = lgb.LGBMRanker(
        objective="lambdarank",
        n_estimators=500,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=20,
        random_state=rs,
        n_jobs=-1,
        verbose=-1,
        label_gain=[0, 1, 3, 7],  # 0,1,2,3 relevance に対する gain
    )
    ranker.fit(X_filled, y, group=groups)

    # 予測時用の中央値を同梱
    ranker._medians = medians
    ranker._feature_cols = FEATURE_COLS

    model_path = MODEL_DIR / "ranker_lightgbm.joblib"
    joblib.dump(ranker, model_path)
    logger.info(f"保存: {model_path}")

    # 簡易評価: 予測順位と実順位の相関
    try:
        scores = ranker.predict(X_filled)
        df_c["_score"] = scores
        # 各race内でscoreをrank化(降順=1位)
        df_c["_pred_rank"] = df_c.groupby("race_id")["_score"].rank(ascending=False, method="min")
        # 1着当てた率
        top1_correct = int(((df_c["arrival_order"] == 1) & (df_c["_pred_rank"] == 1)).sum())
        n_races = int(df_c["race_id"].nunique())
        top1_hit = top1_correct / n_races if n_races else 0.0
        logger.info(f"  ranker top1的中率: {top1_hit:.4f} ({top1_correct}/{n_races})")
    except Exception as e:
        logger.warning(f"ranker評価失敗: {e}")
        top1_hit = None

    summary = {
        "type": "lambdarank",
        "n_rows": len(y),
        "n_races": int(len(groups)),
        "top1_hit_rate": top1_hit,
    }
    return summary


# ──────────────────────────────────────────────
# 内部実装
# ──────────────────────────────────────────────

def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # 特徴量の数値変換
    for col in FEATURE_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = np.nan
    return df


def _make_estimators(config: dict) -> dict:
    rs = config["model"].get("random_state", 42)
    return {
        "logreg": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000, C=0.1, random_state=rs)),
        ]),
        "randomforest": RandomForestClassifier(
            n_estimators=300, max_depth=8, min_samples_leaf=20,
            class_weight="balanced", random_state=rs, n_jobs=-1,
        ),
        "lightgbm": lgb.LGBMClassifier(
            n_estimators=500, learning_rate=0.05, max_depth=6,
            num_leaves=31, subsample=0.8, colsample_bytree=0.8,
            min_child_samples=20, class_weight="balanced",
            random_state=rs, n_jobs=-1, verbose=-1,
        ),
        "catboost": CatBoostClassifier(
            iterations=500, learning_rate=0.05, depth=6,
            l2_leaf_reg=3, random_seed=rs,
            class_weights={0: 1, 1: 5},
            verbose=0,
        ),
    }


def _train_target(
    X: np.ndarray, y: np.ndarray, dates: np.ndarray, target: str, config: dict
) -> tuple[dict, object, str]:
    n_folds = config["model"].get("cv_folds", 5)
    tscv = TimeSeriesSplit(n_splits=n_folds)
    estimators = _make_estimators(config)

    all_scores: dict[str, list] = {name: [] for name in estimators}
    oof_probs: dict[str, np.ndarray] = {name: np.zeros(len(y)) for name in estimators}
    feature_importances: dict[str, list] = {name: [] for name in estimators}

    for fold, (tr_idx, va_idx) in enumerate(tscv.split(X), 1):
        X_tr, X_va = X[tr_idx], X[va_idx]
        y_tr, y_va = y[tr_idx], y[va_idx]

        # 欠損を中央値で補完（fold ごと、訓練データの中央値のみ使用）
        medians = np.nanmedian(X_tr, axis=0)
        X_tr = np.where(np.isnan(X_tr), medians, X_tr)
        X_va = np.where(np.isnan(X_va), medians, X_va)

        for name, est in estimators.items():
            try:
                est.fit(X_tr, y_tr)
                proba = est.predict_proba(X_va)[:, 1]
                oof_probs[name][va_idx] = proba

                score = {
                    "fold": fold,
                    "n_train": int(len(y_tr)),
                    "n_val": int(len(y_va)),
                    "pos_rate": float(y_va.mean()),
                    "logloss": float(log_loss(y_va, proba)),
                    "brier": float(brier_score_loss(y_va, proba)),
                    "auc": float(roc_auc_score(y_va, proba)) if y_va.sum() > 0 else 0.5,
                }
                all_scores[name].append(score)
                logger.info(f"  {target}/{name}/fold{fold}: LogLoss={score['logloss']:.4f} AUC={score['auc']:.4f}")

                # 特徴量重要度（LightGBM/CatBoost/RandomForest のみ）
                base = est.named_steps["clf"] if hasattr(est, "named_steps") else est
                if hasattr(base, "feature_importances_"):
                    feature_importances[name].append(base.feature_importances_.tolist())

            except Exception as e:
                logger.warning(f"  {target}/{name}/fold{fold} 失敗: {e}")

    # 最良モデルを選択（OOF LogLoss 最小）
    summary = {}
    best_name = "lightgbm"
    best_logloss = float("inf")

    for name, scores in all_scores.items():
        if not scores:
            continue
        mean_ll = float(np.mean([s["logloss"] for s in scores]))
        mean_bs = float(np.mean([s["brier"] for s in scores]))
        mean_auc = float(np.mean([s["auc"] for s in scores]))
        std_ll  = float(np.std([s["logloss"] for s in scores]))
        std_auc = float(np.std([s["auc"] for s in scores]))

        # 特徴量重要度の平均
        fi = {}
        if feature_importances[name]:
            fi_arr = np.mean(feature_importances[name], axis=0)
            fi = dict(sorted(
                zip(FEATURE_COLS, fi_arr.tolist()),
                key=lambda x: x[1], reverse=True
            ))

        summary[name] = {
            "logloss_mean": mean_ll, "logloss_std": std_ll,
            "brier_mean": mean_bs,
            "auc_mean": mean_auc, "auc_std": std_auc,
            "fold_scores": scores,
            "feature_importance": fi,
        }
        logger.info(
            f"  {target}/{name}: LogLoss={mean_ll:.4f}±{std_ll:.4f}  "
            f"AUC={mean_auc:.4f}±{std_auc:.4f}  Brier={mean_bs:.4f}"
        )
        if mean_ll < best_logloss:
            best_logloss = mean_ll
            best_name = name

    logger.info(f"  >>> {target} 最良モデル: {best_name} (LogLoss={best_logloss:.4f})")

    # 全データで再学習 + キャリブレーション（sklearn 1.4+ 対応）
    medians_full = np.nanmedian(X, axis=0)
    X_full = np.where(np.isnan(X), medians_full, X)
    best_est = _make_estimators(config)[best_name]
    # CalibratedClassifierCV(cv=5) が内部で5分割CVしながら確率を校正する
    cal_model = CalibratedClassifierCV(best_est, cv=5, method="isotonic")
    cal_model.fit(X_full, y)

    # OOF キャリブレーション曲線（診断用）
    oof = oof_probs[best_name]
    mask = oof > 0
    if mask.sum() > 100:
        try:
            frac_pos, mean_pred = calibration_curve(y[mask], oof[mask], n_bins=10)
            summary[best_name]["calibration_curve"] = {
                "mean_pred": mean_pred.tolist(),
                "frac_pos": frac_pos.tolist(),
            }
        except Exception:
            pass

    # 予測時の欠損補完用に中央値を同梱
    cal_model._medians = medians_full
    cal_model._feature_cols = FEATURE_COLS

    return summary, cal_model, best_name
