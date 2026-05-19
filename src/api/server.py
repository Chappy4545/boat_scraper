"""FastAPI サーバー — API + PWA 静的ファイル配信"""
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.utils.helpers import load_config
from src.utils.logger import get_logger, setup_logger
from src.ingestion.database import init_db, get_session
from src.ingestion.models import Race, Bet, Prediction, BacktestResult, Stadium

logger = get_logger(__name__)

app = FastAPI(title="BoatRace Prediction API", version="0.1.0")

WEB_DIR = Path(__file__).parent.parent.parent / "docs"


# ------------------------------------------------------------------
# Pydantic スキーマ
# ------------------------------------------------------------------
class BetOut(BaseModel):
    race_id: int
    bet_type: str
    combination: str
    model_prob: float
    odds: float
    expected_value: float
    recommended_amount: int
    is_pass: bool
    pass_reason: Optional[str]

    class Config:
        from_attributes = True


class PredictionOut(BaseModel):
    boat_no: int
    win_prob: float
    top2_prob: float
    top3_prob: float
    confidence: float

    class Config:
        from_attributes = True


class RaceOut(BaseModel):
    id: int
    race_date: date
    race_no: int
    grade: Optional[str]
    race_type: Optional[str]
    closing_time: Optional[str]
    is_night: bool

    class Config:
        from_attributes = True


# ------------------------------------------------------------------
# API エンドポイント
# ------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/stadiums")
def get_stadiums():
    with get_session() as session:
        stadiums = session.query(Stadium).all()
        return [{"code": s.code, "name": s.name} for s in stadiums]


@app.get("/api/races/{race_date}")
def get_races(race_date: str):
    try:
        d = date.fromisoformat(race_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="日付フォーマット: YYYY-MM-DD")
    with get_session() as session:
        races = session.query(Race).filter(Race.race_date == d).all()
        return [
            {
                "id": r.id,
                "race_date": str(r.race_date),
                "stadium": r.stadium.name if r.stadium else "",
                "race_no": r.race_no,
                "grade": r.grade,
                "race_type": r.race_type,
                "closing_time": r.closing_time,
                "is_night": r.is_night,
            }
            for r in races
        ]


@app.get("/api/predictions/{race_id}")
def get_predictions(race_id: int):
    with get_session() as session:
        preds = session.query(Prediction).filter(Prediction.race_id == race_id).all()
        if not preds:
            raise HTTPException(status_code=404, detail="予測データがありません")
        return [
            {
                "boat_no": p.boat_no,
                "win_prob": p.win_prob,
                "top2_prob": p.top2_prob,
                "top3_prob": p.top3_prob,
                "confidence": p.confidence,
            }
            for p in sorted(preds, key=lambda x: x.boat_no)
        ]


@app.get("/api/bets/recommended")
def get_recommended_bets(race_date: Optional[str] = None):
    with get_session() as session:
        q = session.query(Bet).filter(Bet.is_pass == False, Bet.expected_value >= 1.10)
        if race_date:
            try:
                d = date.fromisoformat(race_date)
                q = q.join(Race).filter(Race.race_date == d)
            except ValueError:
                raise HTTPException(status_code=400, detail="日付フォーマット: YYYY-MM-DD")
        bets = q.order_by(Bet.expected_value.desc()).limit(50).all()
        return [
            {
                "race_id": b.race_id,
                "bet_type": b.bet_type,
                "combination": b.combination,
                "model_prob": b.model_prob,
                "odds": b.odds,
                "expected_value": b.expected_value,
                "recommended_amount": b.recommended_amount,
            }
            for b in bets
        ]


@app.get("/api/bets/today")
def get_bets_today(race_date: Optional[str] = None):
    """買い目一覧（レース・場情報付き）"""
    try:
        d = date.fromisoformat(race_date) if race_date else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="日付フォーマット: YYYY-MM-DD")
    with get_session() as session:
        bets = (
            session.query(Bet, Race, Stadium)
            .join(Race, Bet.race_id == Race.id)
            .join(Stadium, Race.stadium_id == Stadium.id)
            .filter(Race.race_date == d, Bet.is_pass == False)
            .order_by(Race.race_no.asc(), Bet.expected_value.desc())
            .all()
        )
        return [
            {
                "bet_id": b.id,
                "race_id": b.race_id,
                "stadium_name": s.name,
                "race_no": r.race_no,
                "grade": r.grade,
                "race_type": r.race_type,
                "closing_time": r.closing_time,
                "is_night": r.is_night,
                "bet_type": b.bet_type,
                "combination": b.combination,
                "model_prob": b.model_prob,
                "odds": b.odds,
                "expected_value": b.expected_value,
                "recommended_amount": b.recommended_amount,
                "is_hit": b.is_hit,
                "actual_payout": b.actual_payout,
            }
            for b, r, s in bets
        ]


@app.get("/api/races/{race_id}/entries")
def get_race_entries(race_id: int):
    """出走表（選手情報）"""
    from src.ingestion.models import RaceEntry
    with get_session() as session:
        entries = (
            session.query(RaceEntry)
            .filter(RaceEntry.race_id == race_id)
            .order_by(RaceEntry.boat_no)
            .all()
        )
        return [
            {
                "boat_no": e.boat_no,
                "racer_name": e.racer_name,
                "racer_class": e.racer_class,
                "national_win_rate": e.national_win_rate,
                "motor_top2_rate": e.motor_top2_rate,
                "avg_st": e.avg_st,
            }
            for e in entries
        ]


@app.get("/api/performance")
def get_performance():
    """実際の買い目実績（的中・収支）"""
    with get_session() as session:
        all_bets = (
            session.query(Bet)
            .filter(Bet.is_pass == False)
            .all()
        )
        settled = [b for b in all_bets if b.is_hit is not None]
        hits = sum(1 for b in settled if b.is_hit)
        invested = sum(b.recommended_amount or 0 for b in settled)
        returned = sum((b.actual_payout or 0) for b in settled if b.is_hit)
        return {
            "total_bets": len(all_bets),
            "settled_bets": len(settled),
            "hits": hits,
            "hit_rate": round(hits / len(settled), 4) if settled else None,
            "invested": invested,
            "returned": returned,
            "roi": round(returned / invested, 4) if invested else None,
        }


@app.get("/api/status")
def get_status():
    """システム稼働状況"""
    from sqlalchemy import text as sa_text
    with get_session() as session:
        from src.ingestion.database import get_engine
        engine = get_engine()
        with engine.connect() as conn:
            last_race = conn.execute(
                sa_text("SELECT MAX(race_date) FROM races")
            ).scalar()
            race_count = conn.execute(
                sa_text("SELECT COUNT(*) FROM races")
            ).scalar()
            pred_count = conn.execute(
                sa_text("SELECT COUNT(*) FROM predictions")
            ).scalar()
    return {
        "last_collect_date": str(last_race) if last_race else None,
        "total_races": race_count,
        "total_predictions": pred_count,
        "server_time": datetime.now().isoformat(),
    }


@app.get("/api/backtest/latest")
def get_backtest_latest():
    with get_session() as session:
        result = (
            session.query(BacktestResult)
            .order_by(BacktestResult.run_at.desc())
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail="バックテスト結果がありません")
        import json
        summary = json.loads(result.summary_json) if result.summary_json else {}
        return {
            "model_version": result.model_version,
            "date_start": str(result.date_start),
            "date_end": str(result.date_end),
            "total_races": result.total_races,
            "bet_races": result.bet_races,
            "pass_races": result.pass_races,
            "hit_rate": result.hit_rate,
            "roi": result.roi,
            "max_drawdown": result.max_drawdown,
            "max_consecutive_losses": result.max_consecutive_losses,
            "avg_odds": result.avg_odds,
            "summary": summary,
        }


@app.post("/api/run/collect")
async def trigger_collect(background_tasks: BackgroundTasks, target_date: Optional[str] = None):
    """データ収集をバックグラウンドで実行する。"""
    from src.scraping.official import BoatRaceScraper

    d = date.fromisoformat(target_date) if target_date else date.today()
    config = load_config()

    def _run():
        logger.info(f"データ収集開始: {d}")
        with BoatRaceScraper(config) as scraper:
            scraper.collect_day(d)
        logger.info(f"データ収集完了: {d}")

    background_tasks.add_task(_run)
    return {"message": f"{d} のデータ収集をスケジュールしました"}


# ------------------------------------------------------------------
# PWA 静的ファイル配信
# ------------------------------------------------------------------
if WEB_DIR.exists():
    for _sub in ("css", "js", "icons", "data"):
        _d = WEB_DIR / _sub
        _d.mkdir(parents=True, exist_ok=True)
        app.mount(f"/{_sub}", StaticFiles(directory=str(_d)), name=_sub)

    @app.get("/manifest.json")
    def manifest():
        return FileResponse(str(WEB_DIR / "manifest.json"), media_type="application/json")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(str(WEB_DIR / "sw.js"), media_type="application/javascript")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        # API は上のルートで処理済みなのでここに来るのはフロントエンドのルート
        index = WEB_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse({"error": "frontend not found"}, status_code=404)


# ------------------------------------------------------------------
# 起動ヘルパー
# ------------------------------------------------------------------
def start(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    import uvicorn
    config = load_config()
    setup_logger(config["logging"]["level"], config["logging"]["dir"])
    init_db(config)
    uvicorn.run("src.api.server:app", host=host, port=port, reload=reload)
