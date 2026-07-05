"""
スクレイプ済みDataFrameをDBに保存するインジェスト層。
collect_day() の戻り値を受け取り、各テーブルに upsert する。
"""
from __future__ import annotations

import pandas as pd
from datetime import date as date_cls
from sqlalchemy.orm import Session

from src.ingestion.database import get_session
from src.ingestion.models import (
    Stadium, Race, RaceEntry, BeforeInfo, Weather,
    Odds, RaceResult, Payout,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

STADIUM_NAMES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島",
    "05": "多摩川", "06": "浜名湖", "07": "蒲郡", "08": "常滑",
    "09": "津", "10": "三国", "11": "びわこ", "12": "住之江",
    "13": "尼崎", "14": "鳴門", "15": "丸亀", "16": "児島",
    "17": "宮島", "18": "徳山", "19": "下関", "20": "若松",
    "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}


def _code(val) -> str:
    return f"{int(val):02d}"


def _get_or_create_stadium(session: Session, code: str) -> Stadium:
    code = _code(code)
    st = session.query(Stadium).filter_by(code=code).first()
    if not st:
        st = Stadium(code=code, name=STADIUM_NAMES.get(code, f"場{code}"))
        session.add(st)
        session.flush()
    return st


def _get_or_create_race(session: Session, stadium: Stadium,
                         race_date, race_no: int) -> Race:
    race = session.query(Race).filter_by(
        race_date=race_date, stadium_id=stadium.id, race_no=race_no
    ).first()
    if not race:
        race = Race(race_date=race_date, stadium_id=stadium.id, race_no=race_no)
        session.add(race)
        session.flush()
    return race


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _safe_int(v, default=0) -> int:
    try:
        return int(v) if v is not None else default
    except (ValueError, TypeError):
        return default


# ──────────────────────────────────────────────
# 公開保存 API
# ──────────────────────────────────────────────

def save_racelist(df: pd.DataFrame) -> int:
    """出走表 → race_entries"""
    if df is None or df.empty:
        return 0
    count = 0
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                # race-level fields (同じレースの6艇すべてで同値 → 毎回上書きでOK)
                if row.get("grade"):
                    race.grade = str(row["grade"])
                if row.get("race_type"):
                    race.race_type = str(row["race_type"])
                if row.get("title"):
                    race.title = str(row["title"])[:100]
                if row.get("distance"):
                    race.distance = _safe_int(row["distance"])
                if row.get("is_night") is not None:
                    race.is_night = bool(row["is_night"])

                entry = session.query(RaceEntry).filter_by(
                    race_id=race.id, boat_no=_safe_int(row["boat_no"])
                ).first()
                if not entry:
                    entry = RaceEntry(
                        race_id=race.id, boat_no=_safe_int(row["boat_no"])
                    )
                    session.add(entry)

                entry.racer_no = _safe_int(row.get("racer_no"))
                entry.racer_name = str(row.get("racer_name", ""))[:20]
                entry.racer_class = str(row.get("racer_class", ""))[:5]
                entry.branch = str(row.get("branch", ""))[:10]
                entry.age = _safe_int(row.get("age"))
                entry.weight = _safe_float(row.get("weight"))
                entry.f_count = _safe_int(row.get("f_count"))
                entry.l_count = _safe_int(row.get("l_count"))
                entry.avg_st = _safe_float(row.get("avg_st"))
                entry.national_win_rate = _safe_float(row.get("national_win_rate"))
                entry.national_top2_rate = _safe_float(row.get("national_top2_rate"))
                entry.national_top3_rate = _safe_float(row.get("national_top3_rate"))
                entry.local_win_rate = _safe_float(row.get("local_win_rate"))
                entry.local_top2_rate = _safe_float(row.get("local_top2_rate"))
                entry.local_top3_rate = _safe_float(row.get("local_top3_rate"))
                entry.motor_no = _safe_int(row.get("motor_no"))
                entry.motor_top2_rate = _safe_float(row.get("motor_top2_rate"))
                entry.motor_top3_rate = _safe_float(row.get("motor_top3_rate"))
                entry.boat_no_equipment = _safe_int(row.get("boat_no_equipment"))
                entry.boat_top2_rate = _safe_float(row.get("boat_top2_rate"))
                entry.boat_top3_rate = _safe_float(row.get("boat_top3_rate"))
                count += 1
            except Exception as e:
                logger.warning(f"save_racelist row error: {e}")
    return count


def save_before_info(df: pd.DataFrame) -> int:
    """直前情報 → before_info"""
    if df is None or df.empty:
        return 0
    count = 0
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                bi = session.query(BeforeInfo).filter_by(
                    race_id=race.id, boat_no=_safe_int(row["boat_no"])
                ).first()
                if not bi:
                    bi = BeforeInfo(
                        race_id=race.id, boat_no=_safe_int(row["boat_no"])
                    )
                    session.add(bi)

                bi.entry_course = _safe_int(row.get("entry_course"))
                bi.exhibition_time = _safe_float(row.get("exhibition_time"))
                bi.exhibition_st = _safe_float(row.get("exhibition_st"))
                bi.tilt = _safe_float(row.get("tilt"))
                bi.propeller_changed = bool(row.get("propeller_changed", False))
                bi.parts_changed = str(row.get("parts_changed", ""))[:200]
                bi.weight_diff = _safe_float(row.get("weight_diff"))
                count += 1
            except Exception as e:
                logger.warning(f"save_before_info row error: {e}")
    return count


def save_weather(df: pd.DataFrame) -> int:
    """気象情報 → weather"""
    if df is None or df.empty:
        return 0
    count = 0
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                wt = session.query(Weather).filter_by(race_id=race.id).first()
                if not wt:
                    wt = Weather(race_id=race.id)
                    session.add(wt)

                wt.weather = str(row.get("weather", ""))[:20]
                wt.temperature = _safe_float(row.get("temperature"))
                wt.water_temperature = _safe_float(row.get("water_temperature"))
                wt.wind_direction = str(row.get("wind_direction", ""))[:10]
                wt.wind_speed = _safe_float(row.get("wind_speed"))
                wt.wave_height = _safe_int(row.get("wave_height"))
                count += 1
            except Exception as e:
                logger.warning(f"save_weather row error: {e}")
    return count


def save_odds(df: pd.DataFrame, is_final: bool = True) -> int:
    """オッズ → odds"""
    if df is None or df.empty:
        return 0
    count = 0
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                combo = str(row.get("combination", ""))
                bet_type = str(row.get("bet_type", ""))
                if not combo or not bet_type:
                    continue
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                existing = session.query(Odds).filter_by(
                    race_id=race.id,
                    bet_type=bet_type,
                    combination=combo,
                    is_final=is_final,
                ).first()
                if existing:
                    existing.odds = _safe_float(row.get("odds"))
                else:
                    session.add(Odds(
                        race_id=race.id,
                        bet_type=bet_type,
                        combination=combo,
                        odds=_safe_float(row.get("odds")),
                        is_final=is_final,
                    ))
                count += 1
            except Exception as e:
                logger.warning(f"save_odds row error: {e}")
    return count


def save_race_result(df: pd.DataFrame) -> int:
    """着順 → race_results"""
    if df is None or df.empty:
        return 0
    count = 0
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                rr = session.query(RaceResult).filter_by(
                    race_id=race.id,
                    arrival_order=_safe_int(row["arrival_order"])
                ).first()
                if not rr:
                    rr = RaceResult(
                        race_id=race.id,
                        arrival_order=_safe_int(row["arrival_order"])
                    )
                    session.add(rr)

                rr.boat_no = _safe_int(row.get("boat_no"))
                rr.racer_no = _safe_int(row.get("racer_no"))
                rr.race_time = _safe_float(row.get("race_time"))
                session.flush()  # 全フィールド設定後にflush
                count += 1
            except Exception as e:
                logger.warning(f"save_race_result row error: {e}")
    return count


def save_payouts(df: pd.DataFrame) -> int:
    """払戻 → payouts"""
    if df is None or df.empty:
        return 0
    count = 0
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                combo = str(row.get("combination", ""))
                bet_type = str(row.get("bet_type", ""))
                if not combo or not bet_type:
                    continue
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                existing = session.query(Payout).filter_by(
                    race_id=race.id, bet_type=bet_type, combination=combo
                ).first()
                if existing:
                    existing.payout = _safe_int(row.get("payout"))
                else:
                    session.add(Payout(
                        race_id=race.id,
                        bet_type=bet_type,
                        combination=combo,
                        payout=_safe_int(row.get("payout")),
                    ))
                count += 1
            except Exception as e:
                logger.warning(f"save_payouts row error: {e}")
    return count


def save_day(data: dict) -> dict:
    """collect_day() の戻り値を受け取り、全テーブルに保存する。"""
    summary = {}

    def _save(key: str, fn, *args):
        df = data.get(key)
        if df is not None and not df.empty:
            n = fn(df, *args)
            summary[key] = n
            logger.info(f"  [{key}] {n} 件保存")

    _save("racelist", save_racelist)
    _save("before_info", save_before_info)
    _save("weather", save_weather)
    _save("odds_sanrentan", save_odds, True)
    _save("odds_sanrenfuku", save_odds, True)
    _save("odds_nirentan", save_odds, True)
    _save("odds_nirenfuku", save_odds, True)
    _save("odds_tansho", save_odds, True)
    _save("race_result", save_race_result)
    _save("payouts", save_payouts)

    return summary
