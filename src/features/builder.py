"""
特徴量ビルダー — 指南書 Step 3 の全特徴量を構築する。

DB から race_entries / before_info / weather / odds / race_results を結合し、
モデル学習・予測に使える行=艇単位の DataFrame を返す。
"""
from __future__ import annotations

import pandas as pd
import numpy as np
from sqlalchemy import text
from src.ingestion.database import get_engine
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ──────────────────────────────────────────────
# 公開 API
# ──────────────────────────────────────────────

def build_features(
    date_from: str | None = None,
    date_to: str | None = None,
    include_target: bool = True,
) -> pd.DataFrame:
    """
    学習・バックテスト用の特徴量 DataFrame を返す。

    Parameters
    ----------
    date_from / date_to : 'YYYY-MM-DD' 形式。None なら全期間。
    include_target      : True なら target_win / top2 / top3 列を付加（結果が必要）。
    """
    engine = get_engine()
    raw = _load_raw(engine, date_from, date_to)
    if raw.empty:
        logger.warning("特徴量構築: 対象データなし")
        return pd.DataFrame()

    df = _merge_all(raw)
    df = _add_market_features(df, engine)
    df = _add_stadium_course_features(df, engine)
    if include_target:
        df = _add_targets(df, engine)

    df = _finalize(df)
    logger.info(f"特徴量構築完了: {len(df)} 行, {len(df.columns)} 列")
    return df


def build_features_for_race(race_id: int) -> pd.DataFrame:
    """単一レースの予測用特徴量（target なし）。"""
    engine = get_engine()
    raw = _load_raw_by_race(engine, race_id)
    if raw.empty:
        return pd.DataFrame()
    df = _merge_all(raw)
    df = _add_market_features(df, engine)
    df = _add_stadium_course_features(df, engine)
    return _finalize(df)


# ──────────────────────────────────────────────
# 内部実装
# ──────────────────────────────────────────────

def _load_raw(engine, date_from, date_to) -> pd.DataFrame:
    where_clauses = []
    params: dict = {}
    if date_from:
        where_clauses.append("r.race_date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where_clauses.append("r.race_date <= :date_to")
        params["date_to"] = date_to
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
    SELECT
        r.id            AS race_id,
        r.race_date,
        r.race_no,
        r.grade,
        r.race_type,
        r.is_night,
        r.distance,
        s.code          AS stadium_code,
        s.name          AS stadium_name,
        s.water_type,
        s.tidal_diff,
        e.boat_no,
        e.racer_no,
        e.racer_class,
        e.branch,
        e.age,
        e.weight        AS racer_weight,
        e.f_count,
        e.l_count,
        e.avg_st,
        e.national_win_rate,
        e.national_top2_rate,
        e.national_top3_rate,
        e.local_win_rate,
        e.local_top2_rate,
        e.local_top3_rate,
        e.motor_no,
        e.motor_top2_rate,
        e.motor_top3_rate,
        e.boat_no_equipment,
        e.boat_top2_rate,
        e.boat_top3_rate,
        b.entry_course,
        b.exhibition_time,
        b.exhibition_st,
        b.exhibition_rank,
        b.tilt,
        b.propeller_changed,
        b.weight_diff,
        w.weather,
        w.temperature,
        w.water_temperature,
        w.wind_direction,
        w.wind_speed,
        w.wave_height
    FROM races r
    JOIN stadiums s ON r.stadium_id = s.id
    JOIN race_entries e ON e.race_id = r.id
    LEFT JOIN before_info b ON b.race_id = r.id AND b.boat_no = e.boat_no
    LEFT JOIN weather w ON w.race_id = r.id
    {where}
    ORDER BY r.race_date, s.code, r.race_no, e.boat_no
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def _load_raw_by_race(engine, race_id: int) -> pd.DataFrame:
    return _load_raw.__wrapped__(engine, None, None) if False else _load_raw_for_race_id(engine, race_id)


def _load_raw_for_race_id(engine, race_id: int) -> pd.DataFrame:
    sql = """
    SELECT
        r.id AS race_id, r.race_date, r.race_no, r.grade, r.race_type,
        r.is_night, r.distance,
        s.code AS stadium_code, s.name AS stadium_name, s.water_type, s.tidal_diff,
        e.boat_no, e.racer_no, e.racer_class, e.branch, e.age,
        e.weight AS racer_weight, e.f_count, e.l_count, e.avg_st,
        e.national_win_rate, e.national_top2_rate, e.national_top3_rate,
        e.local_win_rate, e.local_top2_rate, e.local_top3_rate,
        e.motor_no, e.motor_top2_rate, e.motor_top3_rate,
        e.boat_no_equipment, e.boat_top2_rate, e.boat_top3_rate,
        b.entry_course, b.exhibition_time, b.exhibition_st,
        b.exhibition_rank, b.tilt, b.propeller_changed, b.weight_diff,
        w.weather, w.temperature, w.water_temperature,
        w.wind_direction, w.wind_speed, w.wave_height
    FROM races r
    JOIN stadiums s ON r.stadium_id = s.id
    JOIN race_entries e ON e.race_id = r.id
    LEFT JOIN before_info b ON b.race_id = r.id AND b.boat_no = e.boat_no
    LEFT JOIN weather w ON w.race_id = r.id
    WHERE r.id = :race_id
    ORDER BY e.boat_no
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params={"race_id": race_id})


def _merge_all(df: pd.DataFrame) -> pd.DataFrame:
    """基本特徴量の加工。"""
    # 枠番別インネン (1コースが強いことへの補正はモデルに任せる)
    df = df.copy()

    # 級別をカテゴリ数値化
    class_map = {"A1": 4, "A2": 3, "B1": 2, "B2": 1}
    df["racer_class_num"] = df["racer_class"].map(class_map).fillna(1)

    # 水質を数値化 (0=淡水, 1=海水)
    df["is_saltwater"] = (df["water_type"] == "海水").astype(int)

    # ナイター
    df["is_night"] = df["is_night"].fillna(0).astype(int)

    # グレード数値化
    grade_map = {"SG": 6, "PGI": 5, "G1": 4, "G2": 3, "G3": 2, "一般": 1}
    df["grade_num"] = df["grade"].map(grade_map).fillna(1)

    # 風速区間（強風はスタート乱れやすい）
    df["wind_strong"] = (df["wind_speed"].fillna(0) >= 5).astype(int)

    # 天気を数値化（晴=0, 曇=1, 雨=2, その他=3）
    weather_map = {"晴": 0, "晴一時曇": 0, "曇": 1, "曇一時雨": 1, "雨": 2, "雪": 2}
    df["weather_num"] = df["weather"].map(weather_map).fillna(3).astype(int) \
        if "weather" in df.columns else 3

    # 風向を数値化（1〜16: 北基準時計回り22.5°刻み、0=不明）
    df["wind_direction_num"] = pd.to_numeric(
        df["wind_direction"], errors="coerce"
    ).fillna(0).astype(int) if "wind_direction" in df.columns else 0

    # 開催場コードを数値化（"01"〜"24" → 1〜24）
    df["stadium_code_num"] = pd.to_numeric(
        df["stadium_code"], errors="coerce"
    ).fillna(0).astype(int) if "stadium_code" in df.columns else 0

    # 展示タイム偏差（同レース内の相対値）
    df["exh_time_rank"] = df.groupby("race_id")["exhibition_time"].rank(ascending=False)
    df["exh_time_z"] = df.groupby("race_id")["exhibition_time"].transform(
        lambda x: (x - x.mean()) / (x.std() + 1e-9)
    )

    # 展示ST偏差
    df["exh_st_rank"] = df.groupby("race_id")["exhibition_st"].rank(ascending=True)

    # モーター・ボート勝率偏差
    df["motor_top2_z"] = df.groupby("race_id")["motor_top2_rate"].transform(
        lambda x: (x - x.mean()) / (x.std() + 1e-9)
    )
    df["boat_top2_z"] = df.groupby("race_id")["boat_top2_rate"].transform(
        lambda x: (x - x.mean()) / (x.std() + 1e-9)
    )

    # 全国勝率偏差（同レース内）
    df["nat_win_z"] = df.groupby("race_id")["national_win_rate"].transform(
        lambda x: (x - x.mean()) / (x.std() + 1e-9)
    )
    df["local_win_z"] = df.groupby("race_id")["local_win_rate"].transform(
        lambda x: (x - x.mean()) / (x.std() + 1e-9)
    )

    # フライング・出遅れ（ペナルティが多いほど不安定）
    df["penalty_total"] = df["f_count"].fillna(0) + df["l_count"].fillna(0)

    # 季節 (月)
    df["month"] = pd.to_datetime(df["race_date"]).dt.month

    return df


def _add_market_features(df: pd.DataFrame, engine) -> pd.DataFrame:
    """単勝オッズから市場の人気順位・市場オッズを付加。"""
    race_ids = df["race_id"].unique().tolist()
    if not race_ids:
        return df

    sql = """
    SELECT race_id, combination, odds
    FROM odds
    WHERE bet_type = 'tansho' AND race_id IN :ids AND is_final = 1
    """
    try:
        with engine.connect() as conn:
            odds_df = pd.read_sql(
                text("SELECT race_id, combination, odds FROM odds WHERE bet_type='tansho' AND is_final=1"),
                conn,
            )
        odds_df = odds_df[odds_df["race_id"].isin(race_ids)]
        odds_df["boat_no"] = odds_df["combination"].astype(int)
        odds_df["popularity"] = odds_df.groupby("race_id")["odds"].rank(ascending=True)
        df = df.merge(
            odds_df[["race_id", "boat_no", "odds", "popularity"]].rename(
                columns={"odds": "tansho_odds"}
            ),
            on=["race_id", "boat_no"],
            how="left",
        )
    except Exception as e:
        logger.warning(f"単勝オッズ取得失敗: {e}")
        df["tansho_odds"] = np.nan
        df["popularity"] = np.nan

    return df


def _add_stadium_course_features(df: pd.DataFrame, engine) -> pd.DataFrame:
    """場別コース成績を付加。"""
    sql = """
    SELECT code,
        course1_win_rate, course2_win_rate, course3_win_rate,
        course4_win_rate, course5_win_rate, course6_win_rate,
        course1_top3_rate, course2_top3_rate, course3_top3_rate,
        course4_top3_rate, course5_top3_rate, course6_top3_rate
    FROM stadiums
    """
    try:
        with engine.connect() as conn:
            st_df = pd.read_sql(text(sql), conn)
        # wide → long 変換して course=entry_course でマージ
        win_rates = {}
        top3_rates = {}
        for i in range(1, 7):
            win_rates[i] = st_df.set_index("code")[f"course{i}_win_rate"]
            top3_rates[i] = st_df.set_index("code")[f"course{i}_top3_rate"]

        def _get_rate(row, rates):
            course = row.get("entry_course")
            if pd.isna(course) or int(course) not in rates:
                return np.nan
            return rates[int(course)].get(row["stadium_code"], np.nan)

        df["stadium_course_win_rate"] = df.apply(lambda r: _get_rate(r, win_rates), axis=1)
        df["stadium_course_top3_rate"] = df.apply(lambda r: _get_rate(r, top3_rates), axis=1)
    except Exception as e:
        logger.warning(f"場別コース成績取得失敗: {e}")
        df["stadium_course_win_rate"] = np.nan
        df["stadium_course_top3_rate"] = np.nan

    return df


def _add_targets(df: pd.DataFrame, engine) -> pd.DataFrame:
    """レース結果から target_win / top2 / top3 を付加。"""
    race_ids = df["race_id"].unique().tolist()
    sql = "SELECT race_id, boat_no, arrival_order FROM race_results"
    try:
        with engine.connect() as conn:
            res = pd.read_sql(text(sql), conn)
        res = res[res["race_id"].isin(race_ids)]
        res["target_win"] = (res["arrival_order"] == 1).astype(int)
        res["target_top2"] = (res["arrival_order"] <= 2).astype(int)
        res["target_top3"] = (res["arrival_order"] <= 3).astype(int)
        df = df.merge(
            res[["race_id", "boat_no", "arrival_order",
                 "target_win", "target_top2", "target_top3"]],
            on=["race_id", "boat_no"],
            how="left",
        )
    except Exception as e:
        logger.warning(f"結果取得失敗: {e}")
        df["target_win"] = np.nan
        df["target_top2"] = np.nan
        df["target_top3"] = np.nan

    return df


# 学習に使う特徴量列
# 注: tidal_diff / stadium_course_*_rate はDBに値なしのため除外
# 注: tansho_odds / popularity は歴史データ収集時にオッズスキップのため除外
# 注: 2026-06-10 — before_info (展示) / weather は 5/21 以降未収集のため学習対象から除外
#     これらは推論時にNaN→中央値で埋まるだけで実質的に信号を持たず、
#     学習データとのミスマッチが推論精度を劣化させていた
FEATURE_COLS = [
    # レース基本
    "race_no", "grade_num", "is_night", "distance", "month",
    # 開催場（場によってコース別有利度が大きく異なる）
    "stadium_code_num",
    # 艇
    "boat_no", "entry_course",
    # 選手
    "racer_class_num", "age", "racer_weight", "f_count", "l_count", "penalty_total",
    "avg_st",
    "national_win_rate", "national_top2_rate", "national_top3_rate",
    "local_win_rate", "local_top2_rate", "local_top3_rate",
    "nat_win_z", "local_win_z",
    # モーター・ボート
    "motor_top2_rate", "motor_top3_rate", "motor_top2_z",
    "boat_top2_rate", "boat_top3_rate", "boat_top2_z",
    # 場属性（stadiums テーブル由来）
    "is_saltwater",
]

TARGET_COLS = ["target_win", "target_top2", "target_top3"]


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """型変換・欠損補完の最終処理。"""
    for col in FEATURE_COLS:
        if col not in df.columns:
            df[col] = np.nan
        elif df[col].dtype == object:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # bool → int
    for col in ["propeller_changed", "is_night", "wind_strong", "is_saltwater",
                "weather_num", "wind_direction_num", "stadium_code_num"]:
        if col in df.columns:
            df[col] = df[col].fillna(0).astype(int)

    return df
