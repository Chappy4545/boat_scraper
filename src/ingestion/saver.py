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


def _opt_float(v) -> float | None:
    """値が無ければ None を返す（`_safe_float` は 0.0 を返すので使えない）。

    「無い」と「0」を区別したい列で使う。odds_upper は範囲表記の賭式
    （複勝・拡連複）にしか存在せず、他は None が正しい。0.0 を入れると
    「上限0倍」という嘘の値になり、EV を出すときに黙って壊れる。
    pandas から来る NaN も None にする（自己不一致で判定）。
    """
    if v is None or v != v:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _safe_int(v, default=0) -> int:
    try:
        return int(v) if v is not None else default
    except (ValueError, TypeError):
        return default


# ──────────────────────────────────────────────
# 公開保存 API
# ──────────────────────────────────────────────

def save_racelist(df: pd.DataFrame) -> int:
    """出走表 → race_entries

    ⚠️ 同一実行内の重複を自前で弾くこと（`save_payouts` と同じ罠）。
    セッションは autoflush=False なので、直前に add した行は下の
    existing クエリに引っかからない。同じ鍵が2回来ると2行 add され、
    **セッションを抜けるとき**に UNIQUE 制約で落ちる。行ごとの except では
    捕まらず、**そのバッチが丸ごと失われる**。
    2026-08-30 に払戻で、2026-09-03 に直前情報で実際に起きた。
    """
    if df is None or df.empty:
        return 0
    count = 0
    seen: set = set()
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
                # 締切予定時刻（"HH:MM"）。買い目カードに発走の目安を出すため。
                # 従来は列だけあって一度も保存されていなかった。
                if row.get("closing_time"):
                    race.closing_time = str(row["closing_time"])[:5]

                key = (race.id, _safe_int(row["boat_no"]))
                if key in seen:
                    continue
                seen.add(key)
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
    """直前情報 → before_info

    ⚠️ 同一実行内の重複を自前で弾くこと。`save_payouts` と**まったく同じ罠**。
    セッションは autoflush=False なので、直前に session.add したものは
    下の existing クエリに引っかからない。同じ (レース, 艇) が2回来ると
    2行 add され、**コミット時に UNIQUE 制約で落ちる**。

    落ちるのはループの外（セッションを抜けるとき）なので、行ごとの except では
    捕まらず、**そのバッチが丸ごと失われる**。

    2026-09-03 のバックフィルで実際に起きた: 8月分が 37% から一歩も進まず、
    ログに `UNIQUE constraint failed: before_info.race_id, before_info.boat_no`。
    8月に払戻で同じ問題を直したのに、**こちらの saver には入れていなかった**。
    → [[project_update_reliability]]（静かに全滅する形の一覧）
    """
    if df is None or df.empty:
        return 0
    count = 0
    seen: set[tuple] = set()
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                key = (race.id, _safe_int(row["boat_no"]))
                if key in seen:
                    continue          # 同一実行内の重複。コミット時に落ちる
                seen.add(key)
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
    """気象情報 → weather

    ⚠️ 同一実行内の重複を自前で弾くこと（`save_payouts` と同じ罠）。
    セッションは autoflush=False なので、直前に add した行は下の
    existing クエリに引っかからない。同じ鍵が2回来ると2行 add され、
    **セッションを抜けるとき**に UNIQUE 制約で落ちる。行ごとの except では
    捕まらず、**そのバッチが丸ごと失われる**。
    2026-08-30 に払戻で、2026-09-03 に直前情報で実際に起きた。
    """
    if df is None or df.empty:
        return 0
    count = 0
    seen: set = set()
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                if race.id in seen:
                    continue
                seen.add(race.id)
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


def save_odds(df: pd.DataFrame, is_final: bool | None = True,
              force_live: bool | None = None) -> int:
    """オッズ → odds

    is_live（レース当日に取得した＝買う時点で見られた値か）を記録する。
    検証で「知り得ない確定オッズ」を使ってしまう事故を防ぐための区別。

    当日取得済みの値は、後日の遡及取得で上書きしない。
    上書きすると「実際に買えた値」が失われ、バックテストが
    レース後の確定値で買い目を選ぶことになる（2026-08-11 に発覚）。

    is_final=None のとき、行ごとに「当日取得なら板(0) / 後日なら確定(1)」と
    振り分ける。ここを固定 True にしていたことが 2026-08-21 に判明した
    データ破損の原因だった:

      朝9時の板は2連複15通りのうち数通りしか値が出ていない。それを
      is_final=1 で保存すると「確定オッズ」の席に座る。レース後の再取得は
      無い12通りを正しく挿入する一方、既にある数通りは上記の防御に阻まれて
      朝の値のまま残る。結果、1レースの確定オッズが別時点の値の混合になり、
      sum(1/オッズ) が 1.35 ではなく 2.0 前後になる（実測 2,047レース）。

    一意制約に is_final が入っているので、板と確定値は別行として共存できる。
    分けて保存すれば、この衝突は起こりようがない。
    """
    if df is None or df.empty:
        return 0
    from datetime import date as _date
    today = _date.today()
    count = 0
    seen: set = set()
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                combo = str(row.get("combination", ""))
                bet_type = str(row.get("bet_type", ""))
                if not combo or not bet_type:
                    continue
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                rd = row["race_date"]
                race = _get_or_create_race(
                    session, stadium, rd, _safe_int(row["race_no"])
                )
                rd_val = rd.date() if hasattr(rd, "date") else rd
                # force_live: 退避JSONの取り込みなど「その日に取った板だが
                # 取り込むのは後日」という場合に、日付から推測させず明示する。
                is_live = (rd_val == today) if force_live is None else bool(force_live)
                # None なら行ごとに振り分ける（当日=板 / 後日=確定）
                row_final = (not is_live) if is_final is None else is_final

                key = (race.id, bet_type, combo, row_final)
                if key in seen:
                    continue
                seen.add(key)
                existing = session.query(Odds).filter_by(
                    race_id=race.id,
                    bet_type=bet_type,
                    combination=combo,
                    is_final=row_final,
                ).first()
                if existing:
                    # 当日取得済みの値を後日の取得で壊さない
                    if existing.is_live and not is_live:
                        continue
                    existing.odds = _safe_float(row.get("odds"))
                    # 範囲表記（複勝・拡連複）の上限。範囲でない賭式では None。
                    # ⚠️ _safe_float は既定値 0.0 を返すので使えない。
                    # 0.0 を入れると「上限0倍」という嘘になる。
                    existing.odds_upper = _opt_float(row.get("odds_upper"))
                    existing.is_live = bool(existing.is_live or is_live)
                else:
                    session.add(Odds(
                        race_id=race.id,
                        bet_type=bet_type,
                        combination=combo,
                        odds=_safe_float(row.get("odds")),
                        odds_upper=_opt_float(row.get("odds_upper")),
                        is_final=row_final,
                        is_live=is_live,
                    ))
                count += 1
            except Exception as e:
                logger.warning(f"save_odds row error: {e}")
    return count


def save_race_result(df: pd.DataFrame) -> int:
    """着順 → race_results

    ⚠️ 同一実行内の重複を自前で弾くこと（`save_payouts` と同じ罠）。
    セッションは autoflush=False なので、直前に add した行は下の
    existing クエリに引っかからない。同じ鍵が2回来ると2行 add され、
    **セッションを抜けるとき**に UNIQUE 制約で落ちる。行ごとの except では
    捕まらず、**そのバッチが丸ごと失われる**。
    2026-08-30 に払戻で、2026-09-03 に直前情報で実際に起きた。

    ⚠️ 鍵は (race_id, arrival_order)。**同着**があると同じ着順が2艇に付く。
    その場合は後の1艇を捨てることになるが、UNIQUE 制約がそうなっている以上
    落ちるよりはよい（落ちるとその日の着順が丸ごと入らない）。
    """
    if df is None or df.empty:
        return 0
    count = 0
    seen: set = set()
    with get_session() as session:
        for _, row in df.iterrows():
            try:
                stadium = _get_or_create_stadium(session, str(row["stadium_code"]))
                race = _get_or_create_race(
                    session, stadium, row["race_date"], _safe_int(row["race_no"])
                )
                key = (race.id, _safe_int(row["arrival_order"]))
                if key in seen:
                    continue
                seen.add(key)
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
    """払戻 → payouts

    ⚠️ 同一実行内の重複を自前で弾くこと。
    セッションは autoflush=False なので、直前に session.add したものは
    下の existing クエリに引っかからない。払戻ページが同じ組を2回返すと
    2行 add され、**コミット時に UNIQUE 制約で落ちる**。

    しかも落ちるのはループの外（セッションを抜けるとき）なので、
    行ごとの except では捕まらず save_day 全体が巻き添えになる。
    2026-08-30 はこれで結果収集が2回止まり、その日の払戻・確定オッズが
    まるごと入らなかった（拡連複は当日しか取れないので危なかった）。
    """
    if df is None or df.empty:
        return 0
    count = 0
    seen: set[tuple] = set()
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
                key = (race.id, bet_type, combo)
                if key in seen:
                    continue          # この実行で既に積んである
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
                seen.add(key)
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
    # None = 行ごとに振り分け。当日取得なら板(is_final=0)、後日なら確定(1)。
    # 以前はここが True 固定で、朝のスカスカな板が「確定オッズ」として
    # 保存されていた（save_odds の説明を参照）。
    _save("odds_sanrentan", save_odds, None)
    _save("odds_sanrenfuku", save_odds, None)
    _save("odds_nirentan", save_odds, None)
    _save("odds_nirenfuku", save_odds, None)
    _save("odds_tansho", save_odds, None)
    # 2026-09-03 追加。それまで複勝の板は odds に0件で、毎日捨てていた。
    # 単勝と同じページなので通信は増えない。
    _save("odds_fukusho", save_odds, None)
    _save("race_result", save_race_result)
    _save("payouts", save_payouts)

    return summary
