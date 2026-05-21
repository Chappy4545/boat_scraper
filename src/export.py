"""DBから静的JSONファイルを生成し docs/data/ に出力する。
毎日の predict 後に実行し、GitHub Pages用データを更新する。
"""
import json
from datetime import date
from pathlib import Path

from src.ingestion.database import get_session, get_engine
from src.ingestion.models import Race, RaceEntry, Prediction, Bet, Stadium, BacktestResult
from src.utils.logger import get_logger

logger = get_logger(__name__)

DOCS_DIR = Path(__file__).parent.parent / "docs"
DATA_DIR = DOCS_DIR / "data"


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def export_day(target_date: date) -> dict:
    """指定日の races / bets JSON を生成して docs/data/ に保存する。"""
    _ensure_data_dir()
    d = target_date

    with get_session() as session:
        races = (
            session.query(Race, Stadium)
            .join(Stadium, Race.stadium_id == Stadium.id)
            .filter(Race.race_date == d)
            .order_by(Stadium.name, Race.race_no)
            .all()
        )
        race_ids = [r.id for r, _ in races]

        # 予測（race_id → {boat_no: {...}}）
        preds_all = (
            session.query(Prediction)
            .filter(Prediction.race_id.in_(race_ids))
            .all()
        ) if race_ids else []
        pred_map: dict[int, list] = {}
        for p in preds_all:
            pred_map.setdefault(p.race_id, []).append({
                "boat_no": p.boat_no,
                "win_prob": round(p.win_prob, 4) if p.win_prob is not None else None,
                "top2_prob": round(p.top2_prob, 4) if p.top2_prob is not None else None,
                "top3_prob": round(p.top3_prob, 4) if p.top3_prob is not None else None,
            })

        # 出走表
        entries_all = (
            session.query(RaceEntry)
            .filter(RaceEntry.race_id.in_(race_ids))
            .order_by(RaceEntry.boat_no)
            .all()
        ) if race_ids else []
        entry_map: dict[int, list] = {}
        for e in entries_all:
            entry_map.setdefault(e.race_id, []).append({
                "boat_no": e.boat_no,
                "racer_name": e.racer_name,
                "racer_class": e.racer_class,
                "national_win_rate": e.national_win_rate,
                "motor_top2_rate": e.motor_top2_rate,
                "avg_st": e.avg_st,
            })

        # races JSON
        races_json = []
        for r, s in races:
            races_json.append({
                "id": r.id,
                "race_date": str(r.race_date),
                "stadium": s.name,
                "race_no": r.race_no,
                "grade": r.grade,
                "race_type": r.race_type,
                "closing_time": r.closing_time,
                "is_night": bool(r.is_night),
                "predictions": pred_map.get(r.id, []),
                "entries": entry_map.get(r.id, []),
            })

        # bets JSON
        bets_raw = (
            session.query(Bet, Race, Stadium)
            .join(Race, Bet.race_id == Race.id)
            .join(Stadium, Race.stadium_id == Stadium.id)
            .filter(Race.race_date == d, Bet.is_pass == False)
            .order_by(Race.race_no, Bet.expected_value.desc())
            .all()
        )
        bets_json = [
            {
                "bet_id": b.id,
                "race_id": b.race_id,
                "stadium_name": s.name,
                "race_no": r.race_no,
                "grade": r.grade,
                "race_type": r.race_type,
                "closing_time": r.closing_time,
                "is_night": bool(r.is_night),
                "bet_type": b.bet_type,
                "combination": b.combination,
                "model_prob": round(b.model_prob, 4) if b.model_prob is not None else None,
                "odds": b.odds,
                "expected_value": round(b.expected_value, 4) if b.expected_value is not None else None,
                "recommended_amount": b.recommended_amount,
                "is_hit": b.is_hit,
                "actual_payout": b.actual_payout,
            }
            for b, r, s in bets_raw
        ]

    date_str = str(d)
    races_path = DATA_DIR / f"races_{date_str}.json"
    bets_path = DATA_DIR / f"bets_{date_str}.json"
    races_path.write_text(json.dumps(races_json, ensure_ascii=False, indent=None), encoding="utf-8")
    bets_path.write_text(json.dumps(bets_json, ensure_ascii=False, indent=None), encoding="utf-8")
    logger.info(f"export: {races_path.name} ({len(races_json)}件), {bets_path.name} ({len(bets_json)}件)")
    return {"races": len(races_json), "bets": len(bets_json)}


def export_performance() -> None:
    """全期間の収支サマリー＋日別実績を docs/data/performance.json に保存する。"""
    _ensure_data_dir()
    from src.ingestion.database import get_engine
    from sqlalchemy import text as sa_text

    with get_session() as session:
        all_bets = session.query(Bet).filter(Bet.is_pass == False).all()
        settled = [b for b in all_bets if b.is_hit is not None]
        hits = sum(1 for b in settled if b.is_hit)
        invested = sum(b.recommended_amount or 0 for b in settled)
        returned = sum(
            int((b.recommended_amount or 0) * (b.actual_payout or 0) / 100)
            for b in settled if b.is_hit
        )

        bt = (
            session.query(BacktestResult)
            .order_by(BacktestResult.run_at.desc())
            .first()
        )
        backtest = None
        if bt:
            backtest = {
                "model_version": bt.model_version,
                "date_start": str(bt.date_start),
                "date_end": str(bt.date_end),
                "total_races": bt.total_races,
                "bet_races": bt.bet_races,
                "hit_rate": bt.hit_rate,
                "roi": bt.roi,
                "max_drawdown": bt.max_drawdown,
                "avg_odds": bt.avg_odds,
            }

    # 日別実績（直近90日・判定済みのみ）
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sa_text("""
            SELECT r.race_date,
                   COUNT(*) AS total_bets,
                   SUM(CASE WHEN b.is_hit = 1 THEN 1 ELSE 0 END) AS hits,
                   SUM(b.recommended_amount) AS invested,
                   SUM(CASE WHEN b.is_hit = 1 THEN CAST(b.recommended_amount * b.actual_payout / 100 AS INTEGER) ELSE 0 END) AS returned
            FROM bets b
            JOIN races r ON b.race_id = r.id
            WHERE b.is_pass = 0 AND b.is_hit IS NOT NULL
            GROUP BY r.race_date
            ORDER BY r.race_date DESC
            LIMIT 90
        """)).fetchall()
    daily = [
        {
            "date": str(r[0]),
            "bets": r[1],
            "hits": r[2] or 0,
            "invested": r[3] or 0,
            "returned": r[4] or 0,
            "roi": round((r[4] or 0) / r[3], 4) if r[3] else None,
        }
        for r in rows
    ]

    perf = {
        "total_bets": len(all_bets),
        "settled_bets": len(settled),
        "hits": hits,
        "hit_rate": round(hits / len(settled), 4) if settled else None,
        "invested": invested,
        "returned": returned,
        "roi": round(returned / invested, 4) if invested else None,
        "backtest": backtest,
        "daily": daily,
    }

    path = DATA_DIR / "performance.json"
    path.write_text(json.dumps(perf, ensure_ascii=False, indent=None), encoding="utf-8")
    logger.info(f"export: {path.name}")
