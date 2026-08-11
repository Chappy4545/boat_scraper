"""エントリーポイント。

使い方:
  python main.py server                             # PWA + API サーバー起動
  python main.py initdb                             # DBテーブル作成
  python main.py update [DATE]                       # 朝一括更新: 出走表+オッズ収集 → 全レース予測
  python main.py collect [DATE]                     # データ収集 (DATE: YYYY-MM-DD, 省略=今日)
  python main.py collect_range DATE_FROM DATE_TO    # 期間一括収集（オッズスキップ・再開可能）
  python main.py backfill_grades                    # 既存レースのグレード情報をバックフィル
  python main.py train [DATE_FROM] [DATE_TO]        # モデル学習
  python main.py predict [DATE]                     # 予測実行 → 自動でexport
  python main.py judge [DATE]                       # 的中判定 → 自動でexport更新
  python main.py archive_odds [DATE]                # オッズをJSON退避（DB不要・クラウド用）
  python main.py ingest_odds [DATE]                 # 退避JSONをDBに取り込む
  python main.py export [DATE]                      # 静的JSONをdocs/data/に出力
  python main.py backtest DATE_FROM DATE_TO         # バックテスト

スケジュール:
  08:00  BoatRaceUpdate08  → python main.py update   (出走表+オッズ → 全レース予測)
  22:30  BoatRaceJudge     → daily_judge.bat          (結果収集 → 判定 → push)
  23:40  GitHub Actions    → archive_odds            (オッズをJSON退避。PC停止時の保険)

オッズは過去日に遡って取得できない（実測）。当日中ならレース終了後も取得可能。
そのため PC の稼働に関係なく、その日のうちにクラウドで退避しておく。
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


def cmd_collect(target_date: date | None = None, max_workers: int = 5,
                skip_before_info: bool = True):
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.database import init_db
    from src.ingestion.saver import save_day
    config = load_config()
    init_db(config)
    d = target_date or date.today()
    logger.info(f"データ収集開始: {d} (並列={max_workers}, 直前情報={'スキップ' if skip_before_info else '収集'})")
    with BoatRaceScraper(config) as scraper:
        data = scraper.collect_day(d, max_workers=max_workers, skip_before_info=skip_before_info)
    for key, df in data.items():
        logger.info(f"  {key}: {len(df)} 件取得")
    logger.info("DB保存中...")
    summary = save_day(data)
    logger.info(f"データ収集完了: {summary}")
    _purge_raw_cache(config)

    # 今日の収集時のみ、直近7日の未取得結果をキャッチアップ
    if target_date is None or target_date == date.today():
        _catchup_missed_results(max_workers=max_workers)


def _catchup_missed_results(lookback_days: int = 14, max_workers: int = 5):
    """直近N日のうち、データが欠けている日をまとめて収集・判定する。

    対象は2種類:
      1. レースはあるが結果が無い日（judge 前に落ちた等）
      2. レース自体が1件も無い日（PC停止でその日を丸ごと取り逃した）

    2 は以前は救えていなかった。旧実装は `race_cnt > 0` を条件にしており、
    PC が止まった日はレースが0件なので永久に対象外だった
    （2026-07-28〜31, 08-08〜09 が実際にこれで欠落した）。

    なお、オッズは過去日に遡れないためここでは復元できない。
    オッズの保全は GitHub Actions の odds_archive が担う。
    """
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.database import get_engine, get_session
    from src.ingestion.saver import save_day
    from sqlalchemy import text as sa_text
    config = load_config()
    engine = get_engine()
    today = date.today()
    targets = []

    for i in range(1, lookback_days + 1):
        d = today - timedelta(days=i)
        with engine.connect() as conn:
            race_cnt = conn.execute(
                sa_text("SELECT COUNT(*) FROM races WHERE race_date = :d"),
                {"d": str(d)}
            ).scalar()
            result_cnt = conn.execute(
                sa_text("""SELECT COUNT(*) FROM race_results rr
                           JOIN races r ON rr.race_id = r.id
                           WHERE r.race_date = :d"""),
                {"d": str(d)}
            ).scalar()
        # レースは存在するが結果がない日 / レース自体を取り逃した日
        if result_cnt == 0:
            targets.append(d)

    if not targets:
        return

    logger.info(f"キャッチアップ: {len(targets)}日分の結果を取得します {targets}")
    for d in sorted(targets):
        try:
            with BoatRaceScraper(config) as scraper:
                data = scraper.collect_day(d, max_workers=max_workers)
            save_day(data)
            logger.info(f"  キャッチアップ完了: {d}")
            cmd_predict(d)
            cmd_judge(d)
        except Exception as e:
            logger.error(f"  キャッチアップ失敗 {d}: {e}")
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
    from src.models.trainer import train_all, train_ranker
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

    # LambdaRank も同時に訓練 (Plackett-Luce用の強さスコア)
    logger.info("LambdaRank 学習開始 (Plackett-Luce用ranker)")
    ranker_summary = train_ranker(df, config)
    logger.info(f"  ranker: {ranker_summary}")


def cmd_predict(target_date: date | None = None):
    from src.ingestion.database import init_db, get_session, get_engine
    from src.ingestion.models import Race, Bet
    from src.models.predictor import predict_race, save_predictions, predict_race_pl
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
    use_ranker = config.get("model", {}).get("use_ranker", False)
    pl_temperature = float(config.get("model", {}).get("pl_temperature", 1.0))

    with get_session() as session:
        races = session.query(Race).filter(Race.race_date == d).all()
        race_ids = [r.id for r in races]

    logger.info(f"{d}: {len(race_ids)} レースを予測・買い目生成 (use_ranker={use_ranker})")
    mm = MoneyManager(config)
    state = mm.new_state()
    bet_count = 0

    # ── 1パス目: 全レースの買い目を出すだけ（賭け金はまだ決めない）
    #    日次予算をレース順に消費すると、朝のレースで枠を使い切って
    #    午後の良い買い目を取り逃す。2026-08-11 の実測では、その方式だと
    #    7-8月が元本割れ(81,450円)した一方、予算配分を最適化すれば黒字だった。
    per_race: list[tuple[int, pd.DataFrame]] = []
    for rid in race_ids:
        try:
            pred_df = predict_race(rid, model_version)
            if pred_df.empty:
                continue
            save_predictions(rid, pred_df)

            odds_df = _load_odds(engine, rid)
            pl_probs = predict_race_pl(rid, temperature=pl_temperature) if use_ranker else None
            bets_df = generate_bets(pred_df, odds_df, config, model_version, pl_probs=pl_probs)
            per_race.append((rid, bets_df))
        except Exception as e:
            logger.warning(f"  race_id={rid} 予測失敗: {e}")

    # ── 2パス目: 買い目を EV 降順に並べ、良いものから日次予算を割り当てる
    candidates = []
    for rid, bets_df in per_race:
        for idx, row in bets_df.iterrows():
            if not row.get("is_pass", True):
                candidates.append((float(row.get("expected_value") or 0), rid, idx))
    candidates.sort(key=lambda c: -c[0])

    amounts: dict[tuple[int, object], int] = {}
    for ev, rid, idx in candidates:
        row = dict(next(b for r, b in per_race if r == rid).loc[idx])
        amt = mm.calc_bet_amount(
            float(row["expected_value"]), float(row["model_prob"]),
            float(row["odds"]), state,
        )
        if amt > 0:
            state.reserve(amt)      # 予算を消費（これが無いと日次上限が効かない）
            amounts[(rid, idx)] = amt

    skipped_budget = len(candidates) - len(amounts)
    if skipped_budget:
        logger.info(f"  日次予算({mm.max_per_day:,}円)超過のため {skipped_budget} 件を見送り")

    # ── 保存
    for rid, bets_df in per_race:
        try:
            with get_session() as session:
                session.query(Bet).filter(
                    Bet.race_id == rid,
                    Bet.model_version == model_version,
                ).delete()
                for idx, row in bets_df.iterrows():
                    is_pass = bool(row.get("is_pass", True))
                    amount = amounts.get((rid, idx), 0)
                    reason = str(row.get("pass_reason", ""))
                    if not is_pass and amount == 0:
                        is_pass, reason = True, "日次予算上限"
                    session.add(Bet(
                        race_id=rid,
                        model_version=model_version,
                        bet_type=str(row.get("bet_type", "")),
                        combination=str(row.get("combination", "")),
                        model_prob=float(row["model_prob"]) if pd.notna(row.get("model_prob")) else None,
                        market_prob=float(row["market_prob"]) if pd.notna(row.get("market_prob")) else None,
                        odds=float(row["odds"]) if pd.notna(row.get("odds")) else None,
                        expected_value=float(row["expected_value"]) if pd.notna(row.get("expected_value")) else None,
                        recommended_amount=amount,
                        is_pass=is_pass,
                        pass_reason=reason[:100],
                    ))
                    if not is_pass:
                        bet_count += 1
        except Exception as e:
            logger.warning(f"  race_id={rid} 保存失敗: {e}")

    logger.info(
        f"予測完了: 推奨買い目 {bet_count} 件 / 投資額 {int(state.day_invested):,} 円"
    )

    # 予測後に自動エクスポート
    from src.export import export_day, export_performance, export_probs, export_meta, export_pdca
    export_day(d)
    export_performance()
    export_probs(d)
    export_pdca()
    export_meta(source="local")


def cmd_collect_results(target_date: date | None = None, max_workers: int = 5):
    """払戻一覧ページから終了済みレースの結果・払戻のみ収集する（22:30 judge 専用）。
    collect コマンドと違い racelist/odds は取得しない。
    """
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.database import init_db
    from src.ingestion.saver import save_day
    config = load_config()
    init_db(config)
    d = target_date or date.today()
    logger.info(f"結果収集開始: {d}")
    with BoatRaceScraper(config) as scraper:
        data = scraper.collect_day_results(d, max_workers=max_workers)
    for key, df in data.items():
        logger.info(f"  {key}: {len(df)} 件取得")
    summary = save_day(data)
    logger.info(f"結果収集完了: {summary}")
    _purge_raw_cache(config)


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
    from src.export import export_day, export_performance, export_pdca
    export_day(d)
    export_performance()
    export_pdca()


def cmd_refresh_odds(target_date: date | None = None, max_workers: int = 5):
    """DBなしでオッズを再取得してbets JSONを更新する（GitHub Actions専用）。
    docs/data/probs_YYYY-MM-DD.json と races_YYYY-MM-DD.json を読んで動く。
    """
    import json
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime, timezone, timedelta
    from pathlib import Path

    from src.scraping.official import BoatRaceScraper
    import pandas as pd

    JST = timezone(timedelta(hours=9))

    config = load_config()
    d = target_date or date.today()
    docs_data = Path("docs/data")
    probs_path = docs_data / f"probs_{d}.json"
    races_path = docs_data / f"races_{d}.json"
    bets_path = docs_data / f"bets_{d}.json"

    if not probs_path.exists():
        logger.error(f"probs JSONなし: {probs_path}  — 先に update を実行してください")
        return
    if not races_path.exists():
        logger.error(f"races JSONなし: {races_path}")
        return

    probs_data = json.loads(probs_path.read_text(encoding="utf-8"))
    races_data = json.loads(races_path.read_text(encoding="utf-8"))
    bets_existing = json.loads(bets_path.read_text(encoding="utf-8")) if bets_path.exists() else []

    now_jst = datetime.now(JST)
    cfg_bet = config["betting"]
    min_ev = cfg_bet["min_expected_value"]
    min_odds = cfg_bet["min_odds"]
    max_odds = cfg_bet["max_odds"]
    max_bets = cfg_bet["max_bets_per_race"]
    fixed_amount = config.get("money_management", {}).get("fixed_bet_amount", 200)
    # bet_type別の条件と、対象の買い式。これを見ていなかったため、
    # 朝の predict で見送った買い目や停止中の買い式が日中に復活していた。
    overrides = cfg_bet.get("bet_type_overrides", {})
    _bt_map = {"2連単": "nirentan", "2連複": "nirenfuku",
               "3連単": "sanrentan", "3連複": "sanrenfuku"}
    allowed_types = {_bt_map.get(t, t) for t in cfg_bet.get("bet_types", [])}

    # 決着済みベットはそのまま保持
    settled_bets = [b for b in bets_existing if b.get("is_hit") is not None]
    settled_race_ids = {b["race_id"] for b in settled_bets}

    # race_id → レースメタデータ
    race_meta = {r["id"]: r for r in races_data}

    # upcoming races（締切が現在時刻より後）
    upcoming_race_ids: list[int] = []
    for race in races_data:
        if race["id"] in settled_race_ids:
            continue
        ct = race.get("closing_time")
        if ct:
            try:
                closing_dt = datetime.strptime(f"{d} {ct}", "%Y-%m-%d %H:%M").replace(
                    tzinfo=JST
                )
                if closing_dt > now_jst:
                    upcoming_race_ids.append(race["id"])
            except Exception:
                upcoming_race_ids.append(race["id"])
        else:
            upcoming_race_ids.append(race["id"])

    logger.info(f"refresh_odds: {d}  upcoming={len(upcoming_race_ids)}レース")

    # probs JSONをrace_idでインデックス化
    probs_by_race = {
        entry["race_id"]: entry
        for entry in probs_data.get("races", [])
        if entry["race_id"] in upcoming_race_ids
    }

    def fetch_race_odds(race_id: int) -> tuple[int, list[dict]]:
        entry = probs_by_race[race_id]
        stadium_code: str = entry["stadium_code"]
        race_no: int = entry["race_no"]
        combinations: list[dict] = entry["combinations"]
        needed = {c["bet_type"] for c in combinations}

        odds_frames: list[pd.DataFrame] = []
        with BoatRaceScraper(config) as sc:
            if "sanrentan" in needed:
                try:
                    odds_frames.append(sc.get_odds_sanrentan(stadium_code, d, race_no))
                except Exception as e:
                    logger.warning(f"sanrentan odds失敗 {stadium_code} R{race_no}: {e}")
            if "sanrenfuku" in needed:
                try:
                    odds_frames.append(sc.get_odds_sanrenfuku(stadium_code, d, race_no))
                except Exception as e:
                    logger.warning(f"sanrenfuku odds失敗 {stadium_code} R{race_no}: {e}")
            if "nirenfuku" in needed or "nirentan" in needed:
                try:
                    odds_frames.append(sc.get_odds_nirenfuku(stadium_code, d, race_no))
                    odds_frames.append(sc.get_odds_nirentan(stadium_code, d, race_no))
                except Exception as e:
                    logger.warning(f"niren odds失敗 {stadium_code} R{race_no}: {e}")

        if not odds_frames:
            return race_id, []

        odds_all = pd.concat(odds_frames, ignore_index=True)
        odds_lookup = {
            (row["bet_type"], row["combination"]): row["odds"]
            for _, row in odds_all.iterrows()
        }

        candidates = []
        for combo in combinations:
            if allowed_types and combo["bet_type"] not in allowed_types:
                continue          # 停止中の買い式（3連単・3連複）を除外
            key = (combo["bet_type"], combo["combination"])
            odds_val = odds_lookup.get(key)
            if odds_val is None or pd.isna(odds_val):
                continue
            mp = combo["model_prob"]
            if mp is None:
                continue

            # bet_type別の条件を適用する。
            # ここを見ていなかったため、朝の predict では見送られた買い目が
            # 日中の更新で復活していた（2026-08-11 実測: 確率7.2%/21.6% の
            # 買い目が min_model_prob=0.30 を無視して JSON に載っていた）。
            ov = overrides.get(combo["bet_type"], {})
            lo = ov.get("min_odds", min_odds)
            hi = ov.get("max_odds", max_odds)
            if not (lo <= odds_val <= hi):
                continue
            if ov.get("min_model_prob") is not None and mp < ov["min_model_prob"]:
                continue
            if ov.get("max_model_prob") is not None and mp > ov["max_model_prob"]:
                continue

            ev = mp * odds_val
            if ev >= ov.get("min_ev", min_ev):
                candidates.append({
                    "bet_type": combo["bet_type"],
                    "combination": combo["combination"],
                    "model_prob": round(mp, 4),
                    "odds": odds_val,
                    "expected_value": round(ev, 4),
                    "_ev": ev,
                })

        candidates.sort(key=lambda x: x["_ev"], reverse=True)
        candidates = candidates[:max_bets]
        for c in candidates:
            del c["_ev"]
        return race_id, candidates

    new_bets: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_race_odds, rid): rid for rid in probs_by_race}
        for future in as_completed(futures):
            rid = futures[future]
            try:
                race_id, race_bets = future.result()
                meta = race_meta.get(race_id, {})
                for b in race_bets:
                    new_bets.append({
                        "bet_id": None,
                        "race_id": race_id,
                        "stadium_name": meta.get("stadium", ""),
                        "race_no": meta.get("race_no"),
                        "grade": meta.get("grade"),
                        "race_type": meta.get("race_type"),
                        "closing_time": meta.get("closing_time"),
                        "is_night": meta.get("is_night"),
                        **b,
                        "recommended_amount": fixed_amount,
                        "is_hit": None,
                        "actual_payout": None,
                    })
            except Exception as e:
                logger.warning(f"race_id={rid} オッズ更新失敗: {e}")

    new_bets.sort(key=lambda b: (b.get("race_no") or 0, -(b.get("expected_value") or 0)))
    all_bets = settled_bets + new_bets
    bets_path.write_text(json.dumps(all_bets, ensure_ascii=False, indent=None), encoding="utf-8")
    logger.info(f"refresh_odds完了: settled={len(settled_bets)}, upcoming={len(new_bets)}")

    from src.export import export_meta
    export_meta(source="github_actions")


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


def cmd_update(target_date: date | None = None, max_workers: int = 5):
    """出走表+オッズを収集して全レース予測を生成する（朝8:00 専用）。
    直前情報はスキップし、全場の全レースを一括処理する。
    """
    import subprocess
    d = target_date or date.today()
    logger.info(f"=== UPDATE 開始: {d} ===")
    cmd_collect(d, max_workers=max_workers, skip_before_info=True)
    cmd_predict(d)
    logger.info(f"=== UPDATE 完了: {d} ===")

    # docs/data/ を自動的に git push
    try:
        subprocess.run(["git", "add", "docs/data/"], check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], capture_output=True
        )
        if result.returncode != 0:
            from src.ingestion.database import get_engine
            from sqlalchemy import text as sa_text
            engine = get_engine()
            with engine.connect() as conn:
                n = conn.execute(sa_text(
                    "SELECT COUNT(*) FROM bets b JOIN races r ON b.race_id=r.id "
                    "WHERE r.race_date=:d AND b.is_pass=0"
                ), {"d": str(d)}).scalar()
            msg = f"auto: update {d} 朝データ ({n}bets)"
            subprocess.run(["git", "commit", "-m", msg], check=True)
            # -X theirs: rebase時に meta.json 等が競合した場合ローカル側を優先
            pull_result = subprocess.run(
                ["git", "pull", "--rebase", "-X", "theirs", "origin", "master"],
                capture_output=True, text=True
            )
            if pull_result.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], check=False)
                logger.error(f"git pull --rebase 失敗: {pull_result.stderr.strip()}")
                logger.error("手動で 'git pull --rebase && git push' を実行してください")
                return
            subprocess.run(["git", "push"], check=True)
            logger.info(f"git push 完了: {msg}")
        else:
            logger.info("git push スキップ: 変更なし")
    except Exception as e:
        logger.error(f"git push 失敗（手動でpushしてください）: {e}")


def cmd_backfill_before_info(date_from: str, date_to: str,
                             max_minutes: int = 55, max_workers: int = 4):
    """直前情報（展示タイム等）を過去日に遡って取得する。

    2026-05-21 に取得を止めていたが、実測でモデルの唯一の伸びしろだった
    （展示タイムを足すと 2連複30%帯の的中率 +1.12pt、対数損失は市場を上回る）。
    オッズと違い遡及取得できることを確認済み。

    未取得の日だけを対象にし、時間上限で自動停止する（再実行で続きから）。
    """
    import time

    from sqlalchemy import text as sa_text

    from src.ingestion.database import init_db, get_engine
    from src.ingestion.saver import save_before_info, save_weather
    from src.scraping.official import BoatRaceScraper

    config = load_config()
    init_db(config)
    engine = get_engine()

    # 未取得の日と、その日の開催場を洗い出す
    sql = """
        SELECT rc.race_date, s.code, COUNT(*)
        FROM races rc
        JOIN stadiums s ON s.id = rc.stadium_id
        LEFT JOIN before_info b ON b.race_id = rc.id
        WHERE rc.race_date BETWEEN :d1 AND :d2 AND b.race_id IS NULL
        GROUP BY rc.race_date, s.code
        ORDER BY rc.race_date DESC, s.code
    """
    with engine.connect() as conn:
        rows = conn.execute(sa_text(sql), {"d1": date_from, "d2": date_to}).fetchall()
    if not rows:
        logger.info("直前情報の未取得データなし")
        return

    targets = [(str(r[0]), str(r[1]), int(r[2])) for r in rows]
    total_races = sum(t[2] for t in targets)
    logger.info(
        f"直前情報バックフィル: {date_from}〜{date_to} "
        f"未取得 {len(targets)} 場日 / {total_races} レース（上限{max_minutes}分）"
    )

    deadline = time.time() + max_minutes * 60
    done_races = 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_stadium(d_str: str, code: str, n_races: int):
        d = date.fromisoformat(d_str)
        bis, wts = [], []
        with BoatRaceScraper(config) as s:
            for rno in range(1, n_races + 1):
                try:
                    bi, wt = s.get_before_info_and_weather(code, d, rno)
                    if bi is not None and not bi.empty:
                        bis.append(bi)
                    if wt is not None and not wt.empty:
                        wts.append(wt)
                except Exception:
                    continue
        return bis, wts

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {}
        for d_str, code, n in targets:
            if time.time() > deadline:
                break
            futures[ex.submit(fetch_stadium, d_str, code, n)] = (d_str, code, n)
        for fut in as_completed(futures):
            d_str, code, n = futures[fut]
            try:
                bis, wts = fut.result()
            except Exception as e:
                logger.warning(f"  {d_str} 場{code} 失敗: {e}")
                continue
            import pandas as pd
            if bis:
                save_before_info(pd.concat(bis, ignore_index=True))
            if wts:
                save_weather(pd.concat(wts, ignore_index=True))
            done_races += n
            logger.info(f"  {d_str} 場{code}: {n}レース 保存（累計 {done_races}/{total_races}）")

    logger.info(f"直前情報バックフィル終了: {done_races} レース分")


def cmd_judge_live(target_date: date | None = None, max_workers: int = 4):
    """終了したレースの結果を日中に bets JSON へ反映する（DB不要 / クラウド用）。

    従来は 22:30 の judge まで結果が一切出なかった。払戻一覧ページを1回叩けば
    終了済みレースが分かるので、日中の更新ごとに確定分だけ判定して書き戻す。

    DB は使わず docs/data/bets_YYYY-MM-DD.json を直接更新するため、
    ローカルPCが動いていなくても GitHub Actions から実行できる。
    夜の judge は DB を正として同じ JSON を再生成するので競合しない。
    """
    import json
    from pathlib import Path

    from src.scraping.official import BoatRaceScraper

    config = load_config()
    d = target_date or date.today()
    bets_path = Path("docs/data") / f"bets_{d}.json"
    if not bets_path.exists():
        logger.error(f"bets JSONなし: {bets_path}")
        return

    bets = json.loads(bets_path.read_text(encoding="utf-8"))
    # 未判定のものに加え、判定済みでも着順が入っていないものを対象にする
    # （着順は後から追加した項目なので、既存データには入っていない）
    pending = [b for b in bets
               if b.get("is_hit") is None or not b.get("result_order")]
    if not pending:
        logger.info(f"judge_live: {d} 反映すべき結果なし")
        return

    # 場名 → 場コード（bets JSON は場名しか持たないため config から逆引き）
    name_to_code = {v: k for k, v in config.get("stadiums", {}).items()}
    need = set()
    for b in pending:
        code = name_to_code.get(b.get("stadium_name"))
        if code:
            need.add((code, int(b["race_no"])))

    with BoatRaceScraper(config) as scraper:
        try:
            html = scraper._fetch_raw(scraper._url("pay"), {"hd": d.strftime("%Y%m%d")})
            finished = set(scraper.parse_pay_summary(html))
        except Exception as e:
            logger.error(f"払戻一覧の取得に失敗: {e}")
            return

    targets = sorted(need & finished)
    logger.info(
        f"judge_live: {d} 未判定{len(pending)}件 / 終了済み{len(finished)}レース "
        f"→ 判定対象 {len(targets)}レース"
    )
    if not targets:
        return

    # (場コード, R) → ({(bet_type, combination): payout}, 着順リスト)
    payout_map: dict[tuple[str, int], dict[tuple[str, str], int]] = {}
    order_map: dict[tuple[str, int], list[int]] = {}

    def fetch(code: str, race_no: int):
        with BoatRaceScraper(config) as s:
            rr, py = s.get_race_result_and_payouts(code, d, race_no)
        m = {}
        for _, row in py.iterrows():
            m[(str(row["bet_type"]), str(row["combination"]))] = int(row["payout"])
        # 着順（1着から順に艇番）。何着だったかを買い目カードに出すため
        order = []
        try:
            for _, row in rr.sort_values("arrival_order").iterrows():
                if row.get("arrival_order") is not None and str(row["boat_no"]).isdigit():
                    order.append(int(row["boat_no"]))
        except Exception:
            pass
        return (code, race_no), m, order

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(fetch, c, r) for c, r in targets]
        for fut in as_completed(futs):
            try:
                key, m, order = fut.result()
                payout_map[key] = m
                order_map[key] = order
            except Exception as e:
                logger.warning(f"  結果取得失敗: {e}")

    judged = hits = 0
    for b in bets:
        code = name_to_code.get(b.get("stadium_name"))
        key = (code, int(b["race_no"]))
        if key not in payout_map:
            continue
        if order_map.get(key):
            b["result_order"] = order_map[key]   # 例: [2, 5, 6, 1, 4, 3]
        if b.get("is_hit") is not None:
            continue                              # 判定済みは着順だけ補う
        pay = payout_map[key].get((str(b["bet_type"]), str(b["combination"])))
        b["is_hit"] = pay is not None
        b["actual_payout"] = pay
        judged += 1
        if pay is not None:
            hits += 1

    bets_path.write_text(json.dumps(bets, ensure_ascii=False), encoding="utf-8")
    logger.info(f"judge_live 完了: {judged}件を判定（的中 {hits}件）→ {bets_path}")


def cmd_archive_odds(target_date: date | None = None, max_workers: int = 3):
    """当日のオッズを JSON に退避する（DB不要 / GitHub Actions 用）。

    オッズは過去日には遡って取得できない（実測: 3週間前の日付は0件）。
    一方、当日中であればレース終了後も取得できる（実測で確認済み）。
    したがってローカルPCの稼働に関係なく、その日のうちにクラウドで
    退避しておけば、ROI検証に使える資産を失わずに済む。

    実行時刻は朝の update (08:00) に合わせること。買い目の判断は朝の
    オッズで行っており、夜の確定オッズを混ぜると検証の前提が変わるため。

    出力: docs/data/odds_raw_YYYY-MM-DD.json.gz (gzipで約1/17)
    後で `python main.py ingest_odds DATE` で DB に取り込む。
    """
    import gzip
    import json
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    import pandas as pd

    from src.scraping.official import BoatRaceScraper

    config = load_config()
    d = target_date or date.today()
    out_path = Path("docs/data") / f"odds_raw_{d}.json.gz"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with BoatRaceScraper(config) as scraper:
        try:
            stadiums = scraper.get_holding_stadiums(d)
        except Exception as e:
            logger.error(f"開催場取得失敗: {e}")
            return
        if not stadiums:
            logger.info(f"{d}: 開催なし")
            return

        def fetch_stadium(code: str) -> list[dict]:
            rows: list[dict] = []
            with BoatRaceScraper(config) as sc:
                for race_no in range(1, 13):
                    for getter in (sc.get_odds_nirenfuku, sc.get_odds_sanrentan,
                                   sc.get_odds_sanrenfuku):
                        try:
                            df = getter(code, d, race_no)
                        except Exception:
                            continue
                        if df is None or df.empty:
                            continue
                        for _, r in df.iterrows():
                            rows.append({
                                "stadium_code": str(r["stadium_code"]),
                                "race_no": int(r["race_no"]),
                                "bet_type": str(r["bet_type"]),
                                "combination": str(r["combination"]),
                                "odds": float(r["odds"]),
                            })
            return rows

        all_rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch_stadium, c): c for c in stadiums}
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    got = fut.result()
                    all_rows.extend(got)
                    logger.info(f"  場{code}: {len(got)} 件")
                except Exception as e:
                    logger.warning(f"  場{code} 失敗: {e}")

    payload = {"race_date": str(d), "count": len(all_rows), "odds": all_rows}
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    out_path.write_bytes(gzip.compress(blob, 9))
    logger.info(
        f"オッズ退避完了: {out_path} ({len(all_rows)} 件, "
        f"{out_path.stat().st_size/1e6:.2f} MB)"
    )


def cmd_ingest_odds(target_date: date | None = None):
    """`archive_odds` が退避した JSON を DB に取り込む。

    ローカルPCが止まっていた日のオッズを、後から DB に復元するために使う。
    """
    import gzip
    import json
    from pathlib import Path

    import pandas as pd

    from src.ingestion.database import init_db
    from src.ingestion.saver import save_odds

    config = load_config()
    init_db(config)
    d = target_date or date.today()
    base = Path("docs/data")
    gz_path, raw_path = base / f"odds_raw_{d}.json.gz", base / f"odds_raw_{d}.json"
    if gz_path.exists():
        payload = json.loads(gzip.decompress(gz_path.read_bytes()).decode("utf-8"))
    elif raw_path.exists():  # 旧形式（非圧縮）
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        logger.error(f"退避JSONなし: {gz_path}")
        return

    rows = payload.get("odds", [])
    if not rows:
        logger.info(f"{d}: 退避オッズ0件")
        return

    df = pd.DataFrame(rows)
    df["race_date"] = pd.to_datetime(payload["race_date"]).date()
    n = save_odds(df, is_final=True)
    logger.info(f"オッズ取込完了: {d} {n} 件")


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
    elif cmd == "update":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        workers = int(args[2]) if len(args) > 2 else 5
        cmd_update(d, max_workers=workers)
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
    elif cmd == "collect_results":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_collect_results(d)
    elif cmd == "refresh_odds":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        workers = int(args[2]) if len(args) > 2 else 5
        cmd_refresh_odds(d, max_workers=workers)
    elif cmd == "judge":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_judge(d)
    elif cmd == "judge_live":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_judge_live(d)
    elif cmd == "backfill_before_info":
        if len(args) < 3:
            print("使い方: python main.py backfill_before_info DATE_FROM DATE_TO [MAX_MINUTES]")
            return
        cmd_backfill_before_info(
            args[1], args[2],
            max_minutes=int(args[3]) if len(args) > 3 else 55,
        )
    elif cmd == "archive_odds":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_archive_odds(d)
    elif cmd == "ingest_odds":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_ingest_odds(d)
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
