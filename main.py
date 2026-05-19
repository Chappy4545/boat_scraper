"""エントリーポイント。

使い方:
  python main.py server                             # PWA + API サーバー起動
  python main.py initdb                             # DBテーブル作成
  python main.py collect [DATE]                     # データ収集 (DATE: YYYY-MM-DD, 省略=今日)
  python main.py collect_range DATE_FROM DATE_TO    # 期間一括収集（オッズスキップ・再開可能）
  python main.py backfill_grades                    # 既存レースのグレード情報をバックフィル
  python main.py train [DATE_FROM] [DATE_TO]        # モデル学習
  python main.py predict [DATE]                     # 予測実行 → 自動でexport
  python main.py judge [DATE]                       # 的中判定 → 自動でexport更新
  python main.py export [DATE]                      # 静的JSONをdocs/data/に出力
  python main.py backtest DATE_FROM DATE_TO         # バックテスト
"""
import sys
from datetime import date, timedelta

from src.utils.helpers import load_config
from src.utils.logger import setup_logger, get_logger

logger = get_logger(__name__)


def cmd_server():
    from src.api.server import start
    config = load_config()
    api_cfg = config.get("api", {})
    start(host=api_cfg.get("host", "0.0.0.0"),
          port=api_cfg.get("port", 8000),
          reload=api_cfg.get("debug", False))


def cmd_initdb():
    from src.ingestion.database import init_db
    config = load_config()
    init_db(config)
    logger.info("DB初期化完了")


def _purge_raw_cache(config: dict) -> None:
    import shutil
    cache_dir = config.get("scraping", {}).get("cache_dir", "data/raw")
    p = __import__("pathlib").Path(cache_dir)
    if p.exists():
        shutil.rmtree(p)
        logger.info(f"HTMLキャッシュ削除: {cache_dir}")


def cmd_collect(target_date: date | None = None, max_workers: int = 5):
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.database import init_db
    from src.ingestion.saver import save_day
    config = load_config()
    init_db(config)
    d = target_date or date.today()
    logger.info(f"データ収集開始: {d} (並列={max_workers})")
    with BoatRaceScraper(config) as scraper:
        data = scraper.collect_day(d, max_workers=max_workers)
    for key, df in data.items():
        logger.info(f"  {key}: {len(df)} 件取得")
    logger.info("DB保存中...")
    summary = save_day(data)
    logger.info(f"データ収集完了: {summary}")
    _purge_raw_cache(config)


def cmd_collect_range(date_from: str, date_to: str,
                      max_minutes: int = 55, max_workers: int = 5,
                      skip_odds: bool = True):
    """期間一括収集。収集済み日はスキップし、max_minutes 分で自動停止。
    再実行すると続きから再開する。
    """
    import time
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.database import init_db, get_engine
    from src.ingestion.saver import save_day
    from sqlalchemy import text as sa_text

    config = load_config()
    init_db(config)
    engine = get_engine()

    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    total_days = (d_to - d_from).days + 1
    start_time = time.time()
    deadline = start_time + max_minutes * 60

    current = d_from
    done = skipped = 0

    logger.info(
        f"一括収集開始: {date_from} 〜 {date_to} ({total_days}日分) "
        f"並列={max_workers}場 上限={max_minutes}分 "
        f"オッズ={'スキップ' if skip_odds else '収集'}"
    )

    while current <= d_to:
        remaining = (deadline - time.time()) / 60
        if remaining <= 0:
            logger.info(f"時間上限({max_minutes}分)に達したため停止。"
                        f"再実行すると {current} から再開します。")
            break

        # 収集済みチェック（レースと結果が両方そろっていれば完了とみなす）
        with engine.connect() as conn:
            cnt = conn.execute(
                sa_text("SELECT COUNT(*) FROM races WHERE race_date = :d"),
                {"d": str(current)}
            ).scalar()
            res_cnt = conn.execute(
                sa_text("""SELECT COUNT(*) FROM race_results rr
                           JOIN races r ON rr.race_id = r.id
                           WHERE r.race_date = :d"""),
                {"d": str(current)}
            ).scalar()
        if cnt >= 50 and res_cnt >= 50:
            skipped += 1
            current += timedelta(days=1)
            continue

        logger.info(f"[{done+skipped+1}/{total_days}] {current} 収集中... "
                    f"(残り約{remaining:.0f}分)")
        try:
            with BoatRaceScraper(config) as scraper:
                data = scraper.collect_day(current, max_workers=max_workers,
                                           skip_odds=skip_odds)
            summary = save_day(data)
            logger.info(f"  完了: {summary}")
        except Exception as e:
            logger.error(f"  {current} 失敗: {e}")

        done += 1
        current += timedelta(days=1)

    logger.info(f"セッション終了: {done}日収集 / {skipped}日スキップ")
    _purge_raw_cache(config)


def cmd_train(date_from: str | None = None, date_to: str | None = None):
    from src.features.builder import build_features
    from src.models.trainer import train_all
    from src.ingestion.database import init_db
    config = load_config()
    init_db(config)
    logger.info(f"特徴量構築中: {date_from} 〜 {date_to}")
    df = build_features(date_from, date_to, include_target=True)
    if df.empty:
        logger.error("学習データなし — まず collect でデータを取得してください")
        return
    logger.info(f"モデル学習開始: {len(df)} 行")
    results = train_all(df, config)
    for target, scores in results.items():
        logger.info(f"  {target}: {scores}")


def cmd_predict(target_date: date | None = None):
    from src.ingestion.database import init_db, get_session, get_engine
    from src.ingestion.models import Race, Bet
    from src.models.predictor import predict_race, save_predictions
    from src.betting.ev_calculator import generate_bets
    from src.betting.money_manager import MoneyManager
    from src.backtest.runner import _load_odds
    from sqlalchemy import text as sa_text
    import pandas as pd

    config = load_config()
    init_db(config)
    d = target_date or date.today()
    engine = get_engine()
    model_version = config.get("model", {}).get("version", "v1")

    with get_session() as session:
        races = session.query(Race).filter(Race.race_date == d).all()
        race_ids = [r.id for r in races]

    logger.info(f"{d}: {len(race_ids)} レースを予測・買い目生成")
    mm = MoneyManager(config)
    state = mm.new_state()
    bet_count = 0

    for rid in race_ids:
        try:
            # 確率予測 & 保存
            pred_df = predict_race(rid, model_version)
            if pred_df.empty:
                continue
            save_predictions(rid, pred_df)

            # オッズ取得
            odds_df = _load_odds(engine, rid)

            # 買い目生成（EV計算）
            bets_df = generate_bets(pred_df, odds_df, config, model_version)

            # bets テーブルへ保存（既存削除→再挿入）
            with get_session() as session:
                session.query(Bet).filter(
                    Bet.race_id == rid,
                    Bet.model_version == model_version,
                ).delete()
                for _, row in bets_df.iterrows():
                    amount = 0
                    if not row.get("is_pass", True):
                        amount = mm.calc_bet_amount(
                            float(row["expected_value"]),
                            float(row["model_prob"]),
                            float(row["odds"]),
                            state,
                        )
                    session.add(Bet(
                        race_id=rid,
                        model_version=model_version,
                        bet_type=str(row.get("bet_type", "")),
                        combination=str(row.get("combination", "")),
                        model_prob=float(row["model_prob"]) if pd.notna(row.get("model_prob")) else None,
                        odds=float(row["odds"]) if pd.notna(row.get("odds")) else None,
                        expected_value=float(row["expected_value"]) if pd.notna(row.get("expected_value")) else None,
                        recommended_amount=amount,
                        is_pass=bool(row.get("is_pass", True)),
                        pass_reason=str(row.get("pass_reason", ""))[:100],
                    ))
                    if not row.get("is_pass", True):
                        bet_count += 1

        except Exception as e:
            logger.warning(f"  race_id={rid} 予測失敗: {e}")

    logger.info(f"予測完了: 推奨買い目 {bet_count} 件")

    # 予測後に自動エクスポート
    from src.export import export_day, export_performance
    export_day(d)
    export_performance()


def cmd_judge(target_date: date | None = None):
    """当日の買い目に的中/外れを記録する。22:00 collect の後に実行する。"""
    from src.ingestion.database import init_db, get_session
    from src.ingestion.models import Bet, Race, Payout, RaceResult
    config = load_config()
    init_db(config)
    d = target_date or date.today()

    with get_session() as session:
        pairs = (
            session.query(Bet, Race)
            .join(Race, Bet.race_id == Race.id)
            .filter(Race.race_date == d, Bet.is_pass == False, Bet.is_hit == None)
            .all()
        )
        judged = 0
        for bet, race in pairs:
            has_result = session.query(RaceResult).filter(
                RaceResult.race_id == race.id
            ).count() > 0
            if not has_result:
                continue
            payout = session.query(Payout).filter(
                Payout.race_id == race.id,
                Payout.bet_type == bet.bet_type,
                Payout.combination == bet.combination,
            ).first()
            bet.is_hit = payout is not None
            bet.actual_payout = payout.payout if payout else None
            judged += 1

    logger.info(f"的中判定完了: {d} {judged}件")

    # 判定後にエクスポートを更新
    from src.export import export_day, export_performance
    export_day(d)
    export_performance()


def cmd_backfill_grades(max_workers: int = 5):
    """grade=NULL の既存レースにグレード・レース種別・タイトルをバックフィルする。
    racelist URL のみフェッチ（オッズ・結果はスキップ）するため軽量。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.ingestion.database import init_db, get_engine, get_session
    from src.ingestion.models import Race, Stadium
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.saver import _safe_int
    from sqlalchemy import text as sa_text

    config = load_config()
    init_db(config)
    engine = get_engine()

    with engine.connect() as conn:
        rows = conn.execute(sa_text("""
            SELECT r.id, r.race_date, r.race_no, s.code
            FROM races r JOIN stadiums s ON r.stadium_id = s.id
            WHERE r.grade IS NULL
            ORDER BY r.race_date, s.code, r.race_no
        """)).fetchall()

    total = len(rows)
    if total == 0:
        logger.info("グレード未設定レースなし — バックフィル不要")
        return
    logger.info(f"バックフィル対象: {total} レース (並列={max_workers})")

    def _fetch_one(race_id, race_date, race_no, stadium_code):
        from datetime import date as date_cls
        d = date_cls.fromisoformat(str(race_date))
        with BoatRaceScraper(config) as s:
            url = s._url("racelist")
            params = s._params(stadium_code, d, race_no)
            html = s._fetch_raw(url, params)
            return race_id, s._parse_race_header(
                __import__("bs4").BeautifulSoup(html, "lxml")
            )

    updated = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_one, r.id, r.race_date, r.race_no, r.code): r.id
            for r in rows
        }
        for i, future in enumerate(as_completed(futures), 1):
            try:
                race_id, hdr = future.result()
                if not hdr.get("grade"):
                    continue
                with get_session() as session:
                    race = session.query(Race).filter_by(id=race_id).first()
                    if race:
                        race.grade = hdr["grade"]
                        race.race_type = hdr.get("race_type")
                        if hdr.get("title"):
                            race.title = hdr["title"][:100]
                        if hdr.get("distance"):
                            race.distance = _safe_int(hdr["distance"])
                        if hdr.get("is_night") is not None:
                            race.is_night = bool(hdr["is_night"])
                        updated += 1
                if i % 100 == 0:
                    logger.info(f"  進捗: {i}/{total} ({updated}件更新済み)")
            except Exception as e:
                logger.warning(f"  race_id={futures[future]} 失敗: {e}")

    _purge_raw_cache(config)
    logger.info(f"バックフィル完了: {updated}/{total} レース更新")


def cmd_backtest(date_from: str, date_to: str):
    from src.backtest.runner import run_backtest
    from src.ingestion.database import init_db
    config = load_config()
    init_db(config)
    summary = run_backtest(date_from, date_to, config=config)
    if summary:
        logger.info(f"回収率: {summary.get('roi', 0)*100:.1f}%  "
                    f"的中率: {summary.get('hit_rate', 0)*100:.1f}%  "
                    f"最大DD: {summary.get('max_drawdown', 0)*100:.1f}%")


def main():
    config = load_config()
    setup_logger(config["logging"]["level"], config["logging"]["dir"])

    args = sys.argv[1:]
    cmd = args[0] if args else "server"

    if cmd == "server":
        cmd_server()
    elif cmd == "initdb":
        cmd_initdb()
    elif cmd == "collect":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        workers = int(args[2]) if len(args) > 2 else 5
        cmd_collect(d, max_workers=workers)
    elif cmd == "backfill_grades":
        workers = int(args[1]) if len(args) > 1 else 5
        cmd_backfill_grades(max_workers=workers)
    elif cmd == "collect_range":
        if len(args) < 3:
            print("使い方: python main.py collect_range DATE_FROM DATE_TO [MAX_MINUTES] [MAX_WORKERS] [SKIP_ODDS=1]")
            sys.exit(1)
        cmd_collect_range(
            args[1], args[2],
            max_minutes=int(args[3]) if len(args) > 3 else 55,
            max_workers=int(args[4]) if len(args) > 4 else 5,
            skip_odds=bool(int(args[5])) if len(args) > 5 else True,
        )
    elif cmd == "train":
        cmd_train(
            args[1] if len(args) > 1 else None,
            args[2] if len(args) > 2 else None,
        )
    elif cmd == "predict":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_predict(d)
    elif cmd == "judge":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_judge(d)
    elif cmd == "export":
        from src.export import export_day, export_performance
        from src.ingestion.database import init_db
        config = load_config()
        init_db(config)
        d = date.fromisoformat(args[1]) if len(args) > 1 else date.today()
        export_day(d)
        export_performance()
    elif cmd == "backtest":
        if len(args) < 3:
            print("使い方: python main.py backtest DATE_FROM DATE_TO")
            sys.exit(1)
        cmd_backtest(args[1], args[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
