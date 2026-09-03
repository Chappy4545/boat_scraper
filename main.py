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
from datetime import date, datetime, timedelta, timezone

# 締切の判定に使う。全国どの場も日本時間で締め切る。
# ⚠️ naive な datetime.now() と混ぜないこと（比較で例外になる）。
JST = timezone(timedelta(hours=9))

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
                skip_before_info: bool = True, skip_odds: bool = False,
                skip_results: bool | None = None):
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.database import init_db
    from src.ingestion.saver import save_day
    config = load_config()
    init_db(config)
    d = target_date or date.today()
    # 当日はまだレースが終わっていないので結果ページはほぼ空で返る
    # （2026-08-22 実測: 156回叩いて中身は6レース分）。着順と払戻は夜の判定が
    # collect_day_results で正しく集める。過去日のキャッチアップでは必ず取る。
    if skip_results is None:
        skip_results = (d == date.today())
    logger.info(f"データ収集開始: {d} (並列={max_workers}, "
                f"直前情報={'スキップ' if skip_before_info else '収集'}, "
                f"結果={'スキップ' if skip_results else '収集'})")
    with BoatRaceScraper(config) as scraper:
        data = scraper.collect_day(d, max_workers=max_workers,
                                   skip_before_info=skip_before_info,
                                   skip_odds=skip_odds,
                                   skip_results=skip_results)
    for key, df in data.items():
        logger.info(f"  {key}: {len(df)} 件取得")
    logger.info("DB保存中...")
    summary = save_day(data)
    logger.info(f"データ収集完了: {summary}")
    _purge_raw_cache(config)


def _workers_arg(args: list[str], default: int = 5) -> int:
    """引数から並列数を拾う。フラグが混ざっていても壊れないようにする。

    位置引数として `int(args[2])` で読んでいたため、
    `main.py update 2026-08-23 --no-predict --no-odds` が
    ValueError: invalid literal for int() ... '--no-predict' で落ちた
    （2026-08-23 の夜のローカル同期が丸ごと失敗）。
    数字に見えるものだけを並列数として拾う。
    """
    for a in args[1:]:
        if a.isdigit():
            return int(a)
    return default


def _archive_odds_rows(data: dict) -> list[dict]:
    """収集結果のうちオッズだけを、退避JSON用の行に落とす。

    ⚠️ ここで落とした列は**永久に戻らない**。オッズは過去日に遡って取得できず、
    使い捨てDBはこの実行が終われば消えるため（[[project_odds_are_perishable]]）。

    複勝・拡連複は `1.0-1.2` の範囲表記で、下限が `odds`・上限が `odds_upper`。
    2026-09-03 まで上限を運んでおらず、毎日捨てていた。
    範囲でない賭式には `odds_upper` が無いので、その場合だけ省く。
    """
    rows: list[dict] = []
    for key, df in data.items():
        if not key.startswith("odds_") or df is None or getattr(df, "empty", True):
            continue
        for _, r in df.iterrows():
            try:
                row = {
                    "stadium_code": str(r["stadium_code"]),
                    "race_no": int(r["race_no"]),
                    "bet_type": str(r["bet_type"]),
                    "combination": str(r["combination"]),
                    "odds": float(r["odds"]),
                }
                # pandas を import していないので、NaN は自己不一致で判定する。
                up = r.get("odds_upper")
                if up is not None and up == up:
                    row["odds_upper"] = float(up)
                rows.append(row)
            except Exception:
                continue
    return rows


def cmd_predict_cloud(target_date: date | None = None, max_workers: int = 5):
    """履歴DBを持たずに当日の予測を作る（GitHub Actions 用）。

    なぜ成立するか: 特徴量はほぼ「同一レース内の順位・偏差」で、素の値は
    出走表ページに印刷されている（全国勝率・当地勝率・モーター/ボート2連率・
    F/L・展示タイム・展示ST）。ほかに要るのは単勝オッズ（人気順）と、
    stadiums の場別コース成績だけ。stadiums は24行・15KB でリポジトリに置ける。
    つまり 385MB の履歴は当日の予測に一切使われていない。

    そこで「その日ぶんだけの使い捨てSQLite」を組み、収集→特徴量→予測→export
    という既存の経路をそのまま通す。新しい特徴量コードを書かないので、
    ローカルとクラウドで結果がずれない。

    履歴DBはこのあとローカルが暇なときに取り込めばよく、朝の時点では要らない。
    これで朝の買い目が PC の稼働状況に依存しなくなる。
    """
    import json as _json
    import os
    from pathlib import Path

    d = target_date or date.today()

    # その日が既に始まっているなら作り直さない。
    #
    # cmd_predict は買い目を入れ直し、export_day が bets_<日付>.json を
    # 書き換える。日中に積み上がった板（確定した買い目・混合候補・判定結果）は
    # そこにしか無いので、走らせ直すと消える。
    # GitHub の schedule はベストエフォートで、同じリポジトリの odds_refresh
    # でも 15〜44 分遅れて起動している（2026-08-22 実測、この日は 23 分以上
    # 経っても起動しなかった）。遅れて昼に動く可能性がある以上、
    # 「いつ呼ばれても安全」にしておく。
    bets_today = Path("docs/data") / f"bets_{d}.json"
    if bets_today.exists():
        try:
            cur = _json.loads(bets_today.read_text(encoding="utf-8"))
        except Exception:
            cur = []
        locked = [b for b in cur if b.get("is_final_pick") or b.get("is_hit") is not None]
        if locked:
            logger.error(
                f"{d} は既に始まっています（確定/判定済み {len(locked)}件）。"
                f"作り直すと日中の記録が消えるため中止します"
            )
            return

    scratch = Path(os.environ.get("BOAT_DB_URL", "").replace("sqlite:///", "")
                   or f"data/cloud_{d}.db")
    os.environ["BOAT_DB_URL"] = f"sqlite:///{scratch.as_posix()}"
    if scratch.exists():
        scratch.unlink()          # 毎回まっさらから。前日の残りを混ぜない

    from src.ingestion.database import init_db, get_session
    from src.ingestion.models import Stadium
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.saver import save_day

    config = load_config()
    init_db(config)
    logger.info(f"クラウド予測: {d}  使い捨てDB={scratch}")

    seed = Path("docs/data/stadiums.json")
    if not seed.exists():
        logger.error(f"{seed} がありません。ローカルで export_stadiums を実行してください")
        return
    rows = _json.loads(seed.read_text(encoding="utf-8"))
    # *_at は JSON では文字列で、SQLAlchemy の DateTime 型が受け取らない。
    # 特徴量では使わないので落とす。
    rows = [{k: v for k, v in r.items() if not k.endswith("_at")} for r in rows]
    with get_session() as session:
        for r in rows:
            session.add(Stadium(**r))
    logger.info(f"  場マスタ投入: {len(rows)}場")

    with BoatRaceScraper(config) as scraper:
        # 朝の予測に着順は要らない。当日はまだ終わっていないので結果ページは
        # ほぼ空で返る（実測 156回叩いて6レース分）。着順は夜の判定が集める。
        data = scraper.collect_day(d, max_workers=max_workers,
                                   skip_before_info=True,
                                   skip_results=(d == date.today()))
    logger.info(f"  収集: " + ", ".join(f"{k}={len(v)}" for k, v in data.items()))
    save_day(data)

    # 使い捨てDBはこの実行が終われば消える。オッズは過去日に遡って取得できない
    # ので、いま集めた板をここで退避しておかないと永久に失われる。
    # archive_odds は独立に取り直す作りだが、同じものを2度取る理由はない。
    # ローカルは後で `python main.py ingest_odds <日付>` で履歴DBに入れる。
    import gzip as _gzip
    rows = _archive_odds_rows(data)
    if rows:
        out = Path("docs/data") / f"odds_raw_{d}.json.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        blob = _json.dumps({"race_date": str(d), "count": len(rows), "odds": rows},
                           ensure_ascii=False).encode("utf-8")
        out.write_bytes(_gzip.compress(blob, 9))
        logger.info(f"  オッズ退避: {out.name} ({len(rows):,}件 / "
                    f"{out.stat().st_size/1e6:.2f} MB)")

    cmd_predict(d, full_export=False)
    logger.info(f"クラウド予測 完了: {d}")


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
    import json
    from pathlib import Path

    from src.scraping.official import BoatRaceScraper
    from src.ingestion.database import get_engine, get_session
    from src.ingestion.saver import save_day
    from sqlalchemy import text as sa_text
    config = load_config()
    engine = get_engine()
    today = date.today()
    targets = []

    # 再収集は直近数日に限る。中止レースは結果が永久に来ないので、
    # 「揃っていない日」を無期限に対象にすると毎朝そこを取りに行き続ける。
    recollect_days = 3
    rejudge = []

    for i in range(1, lookback_days + 1):
        d = today - timedelta(days=i)
        with engine.connect() as conn:
            race_cnt = conn.execute(
                sa_text("SELECT COUNT(*) FROM races WHERE race_date = :d"),
                {"d": str(d)}
            ).scalar()
            # 判定できていない買い目を、着順が既にあるか無いかで分ける。
            # 「その日の着順が何件あるか」では判断できない。中止や欠場は普通に
            # あり、どの日も理論値(レース数×6)には届かないため、件数で見ると
            # 毎朝すべての日を再収集してしまう。
            unjudged = dict(conn.execute(sa_text("""
                SELECT EXISTS (SELECT 1 FROM race_results rr
                               WHERE rr.race_id = b.race_id) AS has_result,
                       COUNT(*)
                  FROM bets b JOIN races r ON r.id = b.race_id
                 WHERE r.race_date = :d AND b.is_pass = 0 AND b.is_hit IS NULL
                 GROUP BY has_result"""), {"d": str(d)}).all())
            bet_cnt = conn.execute(sa_text(
                "SELECT COUNT(*) FROM bets b JOIN races r ON r.id = b.race_id "
                "WHERE r.race_date = :d"), {"d": str(d)}).scalar()
        judgeable = unjudged.get(1, 0)   # 着順あり → 判定を流すだけでよい
        missing   = unjudged.get(0, 0)   # 着順なし → 取りに行かないと判定できない

        if race_cnt == 0:
            # その日を丸ごと取り逃した。レースと結果は遡って復元できるので
            # lookback_days いっぱいまで対象にする（オッズだけは戻せない）。
            targets.append(d)
        elif missing and i <= recollect_days:
            # 夜のレースだけ取り逃した日がここに入る。旧実装は「結果が0件の日」
            # しか見ておらず、一部だけ欠けた日は永久に取り残されていた
            # （2026-08-11 の 5 レース分が2日間判定されないままだった）。
            # 中止レースは着順が永久に来ないので、再収集は直近数日に限る。
            targets.append(d)
        elif bet_cnt == 0 and i <= recollect_days:
            # レースは取れたのに予測が一度も走らなかった日。
            # 上の分類は「未判定の買い目」を数えるので、買い目が1件も無い日は
            # missing も judgeable も 0 になり、どの枝にも入らず静かに残る。
            # 2026-08-19 がこれ: 朝の更新が起動4秒後に休止で凍結され、
            # レース144件・結果48件・買い目0件のまま翌朝まで放置された。
            # 「レースがある＝収集は動いた」ので race_cnt==0 にも該当しない。
            targets.append(d)
        elif judgeable:
            # 着順は揃っているのに未判定＝判定だけ落ちた日。取得は要らない。
            rejudge.append(d)

    for d in sorted(rejudge):
        logger.info(f"キャッチアップ(判定のみ): {d}")
        try:
            cmd_judge(d)
        except Exception as e:
            logger.error(f"  判定失敗 {d}: {e}")

    if not targets:
        return

    logger.info(f"キャッチアップ: {len(targets)}日分の結果を取得します {targets}")
    for d in sorted(targets):
        try:
            with BoatRaceScraper(config) as scraper:
                data = scraper.collect_day(d, max_workers=max_workers)
            save_day(data)
            logger.info(f"  キャッチアップ完了: {d}")
            # その日のうちに作った予測が残っているなら、作り直さない。
            # cmd_predict は既存の買い目を消して入れ直し、probs_<日付>.json
            # も上書きする。2026-08-17 の復旧で 08-16 の朝の予測（690件と
            # probs ファイル）が今日づけの再予測で消えた。
            # レース後に作り直した買い目は、オッズが確定した状態で選ぶことに
            # なるので損益の集計から外れる（BOUGHT の created_at 条件）。
            # つまり作り直すと、その日の記録が丸ごと検証に使えなくなる。
            # 丸ごと取り逃した日（予測が1件も無い日）だけ新規に作る。
            with engine.connect() as conn:
                same_day = conn.execute(sa_text(
                    "SELECT COUNT(*) FROM bets b JOIN races r ON r.id = b.race_id "
                    "WHERE r.race_date = :d AND date(b.created_at) <= r.race_date"
                ), {"d": str(d)}).scalar()
            # クラウドが買い目を作るようになってから、その日の記録は
            # docs/data/bets_<日付>.json にしか無い。DB だけを見ると常に
            # 「予測が1件も無い日」に見えて、毎日レース後の再予測が走る
            # （2026-08-22 実測: 8,140件・8/16 は 7,552件・8/19 は 5,810件）。
            # レース後に作った買い目は確定オッズで選ぶことになるので損益から
            # 外れるが、DB が無駄に太り、集計のたびに created_at の条件を
            # 書き忘れる事故を招く（2026-08-24 に watchdog で実際に発生）。
            cloud_picks = 0
            try:
                _bj = Path("docs/data") / f"bets_{d}.json"
                if _bj.exists():
                    cloud_picks = len(json.loads(_bj.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning(f"  bets JSON を読めませんでした {d}: {e}")
            if same_day or cloud_picks:
                logger.info(
                    f"  予測は当日のものを残す: {d} "
                    f"(DB {same_day}件 / クラウドの記録 {cloud_picks}件)")
            else:
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


def keep_awake() -> None:
    """実行中はPCを寝かせない（Windows のみ）。

    2026-08-14、朝の更新が 08:06 に始まったスリープで 184 分止まり、
    復帰と同時に強制終了された。前夜の判定も 22:30 に起動したあと寝て、
    08:00 に目を覚まし「翌日」を収集していた。

    タスクスケジューラの電源設定（バッテリー時に実行しない等）は 08-11 に
    直したが、あれは「開始できるか」の話で、走っている最中に寝るのは防げない。

    ただしこのPCは S0 低電力アイドル（Modern Standby）で、そこでは
    この宣言が効かない。実測:
        08-15 ES_SYSTEM_REQUIRED のみ  → 08:06 から 48 分停止
        08-16 ES_DISPLAY_REQUIRED 追加 → 08:07 から 52 分停止
    どちらも「抑止 有効」と記録された直後に寝ている。サービス以外の
    プロセスが Modern Standby を止める手段は無い（それができる
    PowerRequestExecutionRequired はサービス専用）。

    実際の原因はバッテリー時のスリープが 3 分だったこと。そちらを
    延ばして解決した（電源プラン側の設定。SUB_SLEEP STANDBYIDLE）。
    この関数は S3 スリープの機種で効くので残すが、これだけに頼らない。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        r = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        logger.info("スリープ抑止: " + ("有効" if r else "失敗"))
    except Exception as e:
        logger.warning(f"スリープ抑止を設定できません: {e}")


def _acquire_train_lock(max_age_min: int = 90):
    """訓練の同時実行を止める。競合したら非ゼロで終わる。

    訓練は data/processed/models を書き換える。2つ走ると最後に書いた方が残り、
    どちらの成果物か分からないものが出来上がる。

    2026-08-23 に実際に起きた: 日曜9:00の週次訓練がPC休止で取りこぼされ、
    起動時に発火。そこへ検証用の訓練（08-10打ち切り）を並行実行してしまい、
    週次タスクが検証用モデルを "auto: retrain" としてコミット・push した。
    翌朝のクラウドが誤ったモデルで買い目を作る一歩手前だった。

    ⚠️ 中止は必ず非ゼロ終了にすること。weekly_train.bat は終了コードを見てから
    git add data/processed/models する。0 で返すと「訓練していないのに
    コミットする」経路が開く。
    """
    import os
    import time
    from datetime import datetime as _dt
    from pathlib import Path

    lock = Path("data/.train.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        age = (time.time() - lock.stat().st_mtime) / 60
        who = lock.read_text(encoding="utf-8", errors="replace").strip()
        if age < max_age_min:
            logger.error(
                f"別の訓練が動いています（{age:.0f}分前に開始: {who}）。"
                f"同じモデルを奪い合うので中止します"
            )
            raise SystemExit(1)
        logger.warning(f"{age:.0f}分前のロックが残っています。落ちた実行とみなして引き継ぎます: {who}")
    lock.write_text(f"pid={os.getpid()} start={_dt.now().isoformat(timespec='seconds')}",
                    encoding="utf-8")
    return lock


def cmd_train(date_from: str | None = None, date_to: str | None = None):
    from src.features.builder import build_features
    from src.models.trainer import train_all, train_ranker
    from src.ingestion.database import init_db

    lock = _acquire_train_lock()
    try:
        _cmd_train_inner(date_from, date_to)
    finally:
        lock.unlink(missing_ok=True)


def _cmd_train_inner(date_from: str | None, date_to: str | None):
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


def cmd_predict(target_date: date | None = None, full_export: bool = True):
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
        closing_of = {r.id: r.closing_time for r in races}

    # 締切を過ぎたレースに賭け金を付けない。
    #
    # ⚠️ 2026-08-31 発見。朝のクラウド実行が、その日いちばん早いレースの
    # 締切に間に合っていない（初回の書き出しが 09:43 頃、最速のレースは
    # 08:48〜09:26 に締切）。実測: 芦屋R2 は締切の46分後、R3 は20分後に
    # 初めて JSON に現れ、どちらも 500円 が付いたまま損益に入っていた。
    # 08-20 以降12日で24本（7%）が「買えなかったのに集計されている」。
    #
    # refresh_odds には同じ守りが入っている（closed_race_ids。2026-08-12 の
    # 「締切を過ぎてから買い目が生える」事故の後）。こちらにも要る。
    closed = _closed_race_ids(d, closing_of)
    if closed:
        logger.info(f"  締切済み {len(closed)}レースには賭け金を付けません")

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
        if rid in closed:
            continue            # 締切済み。買えないものに金額を付けない
        row = dict(next(b for r, b in per_race if r == rid).loc[idx])
        amt = mm.calc_bet_amount(
            float(row["expected_value"]), float(row["model_prob"]),
            float(row["odds"]), state,
        )
        if amt > 0:
            state.reserve(amt)      # 予算を消費（これが無いと日次上限が効かない）
            amounts[(rid, idx)] = amt

    n_closed_cand = sum(1 for _ev, rid, _i in candidates if rid in closed)
    skipped_budget = len(candidates) - len(amounts) - n_closed_cand
    if skipped_budget:
        logger.info(f"  日次予算({mm.max_per_day:,}円)超過のため {skipped_budget} 件を見送り")
    if n_closed_cand:
        logger.info(f"  締切後のため {n_closed_cand} 件を見送り")

    # ── 保存
    for rid, bets_df in per_race:
        try:
            with get_session() as session:
                # 判定済みの結果を引き継ぐ。作り直しで消すと、終わったレースの
                # 的中・外れが失われる（2026-08-11 に実際に発生し、判定済み10件が
                # 消えた）。同じ買い目（賭式・組合せ）なら結果を持ち越す。
                prev = {
                    (b.bet_type, b.combination): (b.is_hit, b.actual_payout)
                    for b in session.query(Bet).filter(
                        Bet.race_id == rid,
                        Bet.model_version == model_version,
                        Bet.is_hit.isnot(None),
                    ).all()
                }
                session.query(Bet).filter(
                    Bet.race_id == rid,
                    Bet.model_version == model_version,
                ).delete()
                for idx, row in bets_df.iterrows():
                    is_pass = bool(row.get("is_pass", True))
                    amount = amounts.get((rid, idx), 0)
                    reason = str(row.get("pass_reason", ""))
                    if not is_pass and amount == 0:
                        # 理由を分けておく。「買えなかった」と「予算で見送った」は
                        # 別の話で、前者は後から集計から外す判断に使う。
                        is_pass, reason = (
                            (True, "締切後") if rid in closed
                            else (True, "日次予算上限"))
                    hit, pay = prev.get(
                        (str(row.get("bet_type", "")), str(row.get("combination", ""))),
                        (None, None),
                    )
                    session.add(Bet(
                        race_id=rid,
                        model_version=model_version,
                        is_hit=hit,
                        actual_payout=pay,
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
    export_probs(d)
    # performance/pdca は履歴DBが要る。クラウドの当日予測は使い捨てDBで動くので
    # ここを呼ぶと空の成績で上書きしてしまう（full_export=False で飛ばす）。
    if full_export:
        export_performance()
        export_pdca()
        # 場マスタはクラウドの当日予測が使う。中身は滅多に変わらないが、
        # 変わったときに置き去りにならないようローカル実行のたびに出し直す。
        from src.export import export_stadiums
        export_stadiums()
    export_meta(source="local" if full_export else "cloud")


def cmd_collect_results(target_date: date | None = None, max_workers: int = 5):
    """払戻一覧ページから終了済みレースの結果・払戻を収集し、確定オッズも揃える。
    collect コマンドと違い racelist は取得しない。
    """
    from src.scraping.official import BoatRaceScraper
    from src.ingestion.database import init_db
    from src.ingestion.saver import save_day
    keep_awake()
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

    # 確定オッズをここで揃える。朝の update が取るのは発売直後の薄いオッズで、
    # 1通りでも欠けるとそのレースは市場確率を作れず検証に使えない。
    # ここを入れる前は 5月以降 15,774 レース中 4,732 レース（30%）しか
    # 2連複15通りが揃っておらず、検証の母数が貯まらなかった。
    # 確定オッズはレース後も公開されているので後から取れる
    #（発売中の途中経過のオッズだけは遡及できない）。
    _backfill_final_odds(d, max_workers=max_workers)
    _purge_raw_cache(config)


# 確定オッズを毎日そろえる賭式。
#
# ⚠️ 2026-08-30 まで nirenfuku だけだった。そのため単勝・3連複・3連単は
# 手で collect を回した日（8/22・8/25・8/29）にしか確定オッズが無く、
# 8日中3日しか揃っていなかった。オッズは後から作れないので、
# **取らなかった日はその賭式の検証標本が永久に失われる**。
#
# ユーザーの目標は「単勝・2連複・3連複・3連単で買い目を出す」ことなので、
# 4賭式すべての確定オッズが要る。1賭式あたり1ページ／レースで、
# 夜間に走るので当日の運用には影響しない。
# ⚠️ kakurenfuku を先頭に置く。他の賭式はレース後も公開され続けるので後日でも
# 回収できるが、拡連複の oddsk ページは翌日には消える。時間切れ（--max-minutes）
# になったとき、取り返せないものから先に確保する。
FINAL_ODDS_BET_TYPES = ("kakurenfuku", "tansho", "nirenfuku", "sanrenfuku", "sanrentan")


def _backfill_final_odds(d: date, max_workers: int = 5) -> None:
    """その日の、確定オッズが揃っていないレースだけ取りに行く。"""
    import subprocess
    import sys as _sys
    from pathlib import Path
    script = Path(__file__).parent / "scripts" / "backfill_final_odds.py"
    if not script.exists():
        logger.warning("backfill_final_odds.py が見つかりません")
        return
    for bt in FINAL_ODDS_BET_TYPES:
        try:
            r = subprocess.run(
                [_sys.executable, str(script), str(d), str(d),
                 "--bet-type", bt, "--workers", str(max_workers),
                 "--max-minutes", "20"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=1800,
            )
            tail = [ln for ln in (r.stdout or "").splitlines() if "完了" in ln]
            logger.info(f"  確定オッズ({bt}): {tail[-1].split('- ')[-1] if tail else 'ログなし'}")
        except Exception as e:
            logger.warning(f"  確定オッズ({bt}) 取得失敗: {e}")


def _closed_race_ids(d, closing_of: dict, now=None) -> set:
    """締切を過ぎたレースの id。**買えないものに金額を付けないため**の判定。

    ⚠️ 賭け金を付けてよいのは「これから締め切るレース」だけ。締切後のオッズは
    確定値なので、そこから選ぶと「レースが終わってから買えたはずの買い目」に
    なる。2026-08-12 に refresh_odds で発覚（表示18本のうち5本が締切後、
    2本は締切の13分後と46分後）。2026-08-31 に**朝の書き出しにも同じ穴**が
    あると分かった。朝のクラウド実行が最速レースの締切に間に合っていない。

    締切時刻が無いレースは「まだ」とみなす。分からないものを締切後と決めて
    金額を落とすと、締切時刻の取得が壊れた日に買い目が全滅する
    （2026-08-29 に closing_time が全消しになった前例がある）。

    過去日は全部締切済み。未来日は全部これから。
    """
    now = now or datetime.now(JST)
    out = set()
    if d > now.date():
        return out
    for rid, ct in closing_of.items():
        if d < now.date():
            out.add(rid)            # 過去日はレースが終わっている
            continue
        if not ct:
            continue                # 分からないものは「まだ」に倒す
        try:
            close = datetime.strptime(f"{d} {ct}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except (ValueError, TypeError):
            continue
        if close <= now:
            out.add(rid)
    return out


# 検証中の候補ルールで出した買い目の見送り理由。買っていないので損益には
# 数えないが、判定はされるので後から成績を測れる。集計から外す目印でもある。
#
# ⚠️ 集計・除外の判定は必ず CANDIDATE_REASONS（複数形）で行うこと。
# 棄却した market_blend の43本が DB に残っており、単一の文字列で比べると
# それが本番ルールの成績に混ざる。
CANDIDATE_REASON = "候補ルール(価値1点)"
# 「記録のみ(賭式検証)」は 2026-08-30 に賭式を6つへ増やしたときに追加した。
# config の bet_types に無い賭式（単勝・複勝・拡連複・3連複・3連単）の行で、
# 賭け金は付けない。候補ルールと同じく **損益には数えないが判定はする**ので
# 同じ枠組みに載せる。
CANDIDATE_REASONS = ("候補ルール(混合)", "候補ルール(縮み補正)", "候補ルール(価値1点)",
                     "記録のみ(賭式検証)")
CANDIDATE_RULES = ("market_blend", "shrink_adj", "top1_value", "record")
# 見送り理由 → JSON の rule 名。DBの行とJSONの行を突き合わせるのに使う。
_DB_REASON_TO_RULE = {"候補ルール(混合)": "market_blend",
                      "候補ルール(縮み補正)": "shrink_adj",
                      "候補ルール(価値1点)": "top1_value",
                      "記録のみ(賭式検証)": "record"}
_RULE_TO_DB_REASON = {v: k for k, v in _DB_REASON_TO_RULE.items()}


def bet_key(race_id, bet_type: str, combination: str, rule: str | None):
    """JSON の買い目と DB の行を突き合わせるキー。

    ⚠️ ルール名を必ず含めること。本番ルールと候補ルールは同じ組合せを選ぶ
    （候補は本番と同条件で EV だけ補正後オッズで計算するため）。
    (レース, 賭式, 組) だけにすると後勝ちで片方が消える。
    2026-08-24 実測: 51本中15本が重複し、そのまま取り込むと実際に買った
    11本が候補(賭け金0)に化けて損益から外れ、候補4本が消えるところだった。
    bets テーブルに一意制約は無いので、2行として持てる。
    """
    return (race_id, bet_type, combination, rule or "r5")


def index_probs_by_race(probs_data: dict, races_data: list[dict],
                        upcoming_race_ids) -> dict:
    """probs JSON を races JSON 側の race_id に合わせて引けるようにする。

    ⚠️ race_id で直に突き合わせてはいけない。probs を書くのはクラウドの
    predict_cloud で、当日ぶんだけの使い捨てSQLite（1から採番）を使う。
    一方 races JSON はローカルが日中に走ると履歴DBの採番で上書きされる。

    2026-08-26/27 実測: 一致 **0/168・0/156**。そのまま突き合わせると
    1レースも引けず、確定済み以外の買い目が消える。実害:

        08/26 13:02 ローカルが判定 → 13:16 クラウド  買い31本 → 11本
        08/27 13:04 ローカルが判定 → 13:10 クラウド  買い23本 → 10本

    しかもエラーは出ない（対象0レースとして静かに終わる）。ローカルが朝に
    走った日（08-28）は採番が揃うので何も起きず、日によって出たり出なかったり
    する。_sync_bets_from_json は同じ理由で既に場名とレース番号で引いている。

    場名が引けないときだけ従来どおり race_id で引く（stadiums.json が無い
    環境でも、採番が揃っていれば今までどおり動く）。
    """
    import json
    from pathlib import Path

    code_to_name: dict[str, str] = {}
    st_path = Path("docs/data") / "stadiums.json"
    if st_path.exists():
        try:
            code_to_name = {s["code"]: s["name"]
                            for s in json.loads(st_path.read_text(encoding="utf-8"))
                            if s.get("code") and s.get("name")}
        except Exception as e:
            logger.warning(f"stadiums.json を読めません（race_id で突き合わせます）: {e}")

    upcoming = set(upcoming_race_ids)
    by_key = {(r.get("stadium"), r.get("race_no")): r["id"] for r in races_data}

    out: dict = {}
    remapped = 0
    for entry in probs_data.get("races", []):
        own = entry.get("race_id")
        rid = by_key.get((code_to_name.get(entry.get("stadium_code")),
                          entry.get("race_no")))
        if rid is None:
            rid = own
        elif rid != own:
            remapped += 1
        if rid not in upcoming:
            continue
        out[rid] = entry
    if remapped:
        logger.warning(
            f"probs と races で race_id の体系が違います。"
            f"{remapped}レースを場とレース番号で対応づけました"
            f"（対象 {len(out)}レース）")
    return out


def _sync_bets_from_json(d: date) -> None:
    """日中に更新された買い目（docs/data/bets_<d>.json）を DB に取り込む。

    その日「実際に買えと表示した」買い目の記録は JSON 側にしかない。オッズの
    再取得は GitHub Actions で 15 分ごとに動くが、そこからローカルの SQLite に
    は書けないためだ。一方 judge は DB を見て判定し、export_day は DB から
    JSON を作り直す。このままだと 22:30 の判定で「朝の買い目」が JSON を
    上書きし、日中に条件を外れて消えた買い目まで結果つきで復活する。

    2026-08-12 に実際に起きた: 20:33 時点で 18 本（うち確定 15 本）だったのが
    22:31 に 30 本・確定 0 本へ戻り、13 本が結果つきで生えた。記録された
    ROI 116% は「朝のリスト」の成績で、買えと表示したものの成績ではない。

    そこで判定の前に JSON を正として DB を合わせる:
      - JSON にある買い目 → DB に反映（オッズ・EV・確定フラグ）
      - JSON から消えた買い目 → is_pass=1 にして損益から外す（行は残す）

    JSON に確定フラグが1つも無い日は、日中の更新が動かなかった日なので
    何もしない（DB の朝の買い目がその日の記録として正しい）。
    """
    import json
    from datetime import datetime, time as dt_time
    from pathlib import Path
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race, Stadium

    path = Path("docs/data") / f"bets_{d}.json"
    if not path.exists():
        return
    try:
        live = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"bets JSON を読めません {path}: {e}")
        return
    if not any(b.get("is_final_pick") for b in live):
        return

    added = dropped = updated = skipped = 0
    with get_session() as session:
        # ⚠️ JSON の race_id を信じてはいけない。
        # クラウドの predict_cloud は当日ぶんだけの使い捨てSQLiteで動くので、
        # そこで振られる race_id は 1 から始まる別の番号体系になる。
        # 2026-08-23 実測: JSON の race_id は 7〜168、ローカルの同日は
        # 36552〜36719。突き合わせに使うと一致せず全件「追加」になり、
        # しかも id=7 はローカルでは 2026-05-17 戸田R7 を指すため、
        # まったく別の日のレースに買い目が挿入されていた（実害 116 件）。
        # 場名とレース番号で引き直す。これは両者で同じ意味を持つ。
        local_race = {
            (s.name, r.race_no): r.id
            for r, s in session.query(Race, Stadium)
            .join(Stadium, Race.stadium_id == Stadium.id)
            .filter(Race.race_date == d).all()
        }
        # キーの作り方と、なぜルール名が要るかは bet_key の説明を参照。
        def _rule_of_db(bet) -> str:
            return _DB_REASON_TO_RULE.get(bet.pass_reason, "r5")

        live_by_key = {}
        for b in live:
            rid = local_race.get((b.get("stadium_name"), b.get("race_no")))
            if rid is None:
                skipped += 1
                continue
            live_by_key[bet_key(rid, b["bet_type"], b["combination"],
                                b.get("rule"))] = b

        rows = (
            session.query(Bet).join(Race, Bet.race_id == Race.id)
            .filter(Race.race_date == d).all()
        )
        seen = set()
        version = rows[0].model_version if rows else "v1"
        for bet in rows:
            key = bet_key(bet.race_id, bet.bet_type, bet.combination,
                          _rule_of_db(bet))
            src = live_by_key.get(key)
            if src is None:
                # 日中にオッズが動いて条件を外れた買い目。買っていないので
                # 損益には数えないが、後から検証できるよう行は残す。
                if not bet.is_pass:
                    bet.is_pass = True
                    bet.pass_reason = "日中に条件を外れた"
                    dropped += 1
                continue
            seen.add(key)
            # 検証中の候補ルールの買い目は買っていない。損益に混ぜないよう
            # 見送り扱いのまま残す（判定はされるので後から成績を測れる）。
            if src.get("rule") in CANDIDATE_RULES:
                bet.is_pass = True
                bet.pass_reason = _RULE_TO_DB_REASON[src["rule"]]
                bet.recommended_amount = 0
            else:
                bet.is_pass = False
                bet.pass_reason = None
                bet.recommended_amount = src.get("recommended_amount") or 0
            bet.odds = src.get("odds")
            bet.expected_value = src.get("expected_value")
            bet.market_prob = src.get("market_prob", bet.market_prob)
            # ⚠️ 確定フラグは **false→true の一方通行**。一度「これを買え」と
            # 画面に出した事実は、あとから読んだ JSON が古くても取り消さない。
            # 2026-09-02 に 住之江5R・福岡11R の確定12本が消えた:
            #   16:45 ローカルが pull（この時点では未確定）
            #   16:49 クラウドが確定させて commit  ← pull の後
            #   16:54 ローカルの judge が 16:45 の JSON を読んで export
            # 取り込む JSON が古いこと自体は避けきれない（クラウドは15分ごとに
            # 書く）ので、フラグの側を単調にして壊れないようにする。
            bet.is_final_pick = bool(bet.is_final_pick or src.get("is_final_pick"))
            # JSON に載っていた＝その日に「買え」と表示した証拠。損益の集計は
            # date(created_at) <= race_date で絞るので、作成日もレース日に合わせる。
            # クラウドが買い目を作るようになってから、ローカルには「当日作った
            # 買い目」が存在せず、キャッチアップが翌日づけで作り直している。
            # そのままだと実際に表示した買い目が全部集計から外れる
            # （2026-08-22 実測: 買い目29本すべてが損益に入っていなかった）。
            if bet.created_at is None or bet.created_at.date() > d:
                bet.created_at = datetime.combine(d, dt_time(12, 0))
            updated += 1

        for key, src in live_by_key.items():
            if key in seen:
                continue
            # 日中に条件を満たして新しく載った買い目。JSON がその日に推奨した
            # 証拠なので、created_at はレース日にする（BOUGHT の条件に合わせる）。
            is_cand = src.get("rule") in CANDIDATE_RULES
            cand_reason = _RULE_TO_DB_REASON.get(src.get("rule"))
            session.add(Bet(
                race_id=key[0], model_version=version,   # ローカルで引き直したid
                bet_type=src["bet_type"], combination=src["combination"],
                model_prob=src.get("model_prob"),
                market_prob=src.get("market_prob"),
                odds=src.get("odds"),
                expected_value=src.get("expected_value"),
                recommended_amount=0 if is_cand else (src.get("recommended_amount") or 0),
                is_pass=is_cand,
                pass_reason=cand_reason if is_cand else None,
                is_final_pick=bool(src.get("is_final_pick")),
                created_at=datetime.combine(d, dt_time(12, 0)),
            ))
            added += 1

    logger.info(
        f"日中の買い目を取り込み: {d} 反映={updated}件 追加={added}件 "
        f"見送りへ={dropped}件" + (f" 場が引けず無視={skipped}件" if skipped else "")
    )
    if skipped:
        # 場名とレース番号でローカルのレースを引けなかった＝その日の racelist が
        # まだ履歴DBに入っていない。黙って捨てると記録が欠ける。
        logger.warning(
            f"{skipped}件は場名/レース番号でレースを特定できず取り込めませんでした。"
            f"先に collect でその日の出走表を入れてください"
        )


def cmd_judge(target_date: date | None = None):
    """当日の買い目に的中/外れを記録する。22:00 collect の後に実行する。"""
    from src.ingestion.database import init_db, get_session
    from src.ingestion.models import Bet, Race, Payout, RaceResult
    config = load_config()
    init_db(config)
    d = target_date or date.today()

    # 判定より先に、日中に更新された買い目を DB へ取り込む。
    # これを飛ばすと朝のリストを判定してしまう（詳細は関数の説明）。
    _sync_bets_from_json(d)

    from sqlalchemy import or_

    with get_session() as session:
        # 見送った買い目も判定する。賭式ごとの比較（単勝/2連複/3連複のどれが
        # 効くか）も「閾値を緩めていたらどうだったか」の後追い検証も、買わな
        # かった買い目の結果が無いと一切できない。ペーパー記録を貯めていたのに
        # is_pass=False で絞っていたため 2026-08-13 まで1件も判定されていなかった。
        # オッズが無いものは当日買えなかった＝評価できないので除く。
        pairs = (
            session.query(Bet, Race)
            .join(Race, Bet.race_id == Race.id)
            .filter(
                Race.race_date == d,
                Bet.is_hit == None,
                or_(Bet.is_pass == False, Bet.odds != None),
            )
            .all()
        )
        # レース単位でまとめて引く。1件ずつ引くと1日 5,000 件で数千クエリになる。
        race_ids = {race.id for _, race in pairs}
        with_result = {
            rid for (rid,) in session.query(RaceResult.race_id)
            .filter(RaceResult.race_id.in_(race_ids)).distinct()
        }
        payouts = {
            (p.race_id, p.bet_type, p.combination): p.payout
            for p in session.query(Payout).filter(Payout.race_id.in_(race_ids))
        }

        judged = 0
        for bet, race in pairs:
            if race.id not in with_result:
                continue
            pay = payouts.get((race.id, bet.bet_type, bet.combination))
            bet.is_hit = pay is not None
            bet.actual_payout = pay
            judged += 1

    logger.info(f"的中判定完了: {d} {judged}件")

    # 判定後にエクスポートを更新。
    #
    # ⚠️ ここで export_day を呼んではいけない。あれは「DBの中身で JSON を
    # 作り直す」もので、履歴DBに無いもの（予測・出走表・締切時刻・買い目）を
    # null で上書きする。2026-08 の1週間で4回それで壊した
    # （src/export.py の fill_results_into_json に経緯）。
    # ローカルの仕事は「JSON を読んでDBへ取り込む」＋「結果だけ書き足す」。
    from src.export import fill_results_into_json, export_performance, export_pdca
    fill_results_into_json(d)
    # performance / pdca は履歴DBからの集計なので、ここで作るのが正しい
    export_performance()
    export_pdca()


def cmd_refresh_odds(target_date: date | None = None, max_workers: int = 5):
    """DBなしでオッズを再取得してbets JSONを更新する（GitHub Actions専用）。
    docs/data/probs_YYYY-MM-DD.json と races_YYYY-MM-DD.json を読んで動く。
    """
    import json
    import math
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import gzip
    from pathlib import Path

    from src.scraping.official import BoatRaceScraper
    import pandas as pd

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
    # 対応表は src/models/plackett_luce.py に集約（賭式を増やすと片方だけ
    # 直して静かに壊れる形だった）。⚠️ 旧版は単勝・拡連複・複勝を持っておらず、
    # config に書いても素通りして無視されていた。
    from src.models.plackett_luce import BET_TYPE_JP as _bt_map
    # 買う賭式＋記録だけする賭式。どちらも買い目としては作り、
    # 賭け金を出すかどうかだけを分ける（下の _paper 参照）。
    _buy = [_bt_map.get(t, t) for t in cfg_bet.get("bet_types", [])]
    _paper = [_bt_map.get(t, t) for t in cfg_bet.get("paper_bet_types", [])]
    allowed_types = set(_buy) | set(_paper)
    buy_types = set(_buy)

    # 検証中の候補ルール（operation.candidate_rule）。閾値は config に
    # 事前登録してある。ここで動かすと後から都合よく変えられるので読むだけ。
    # ルールの中身は src/betting/candidate_rule.py にまとめてある
    # （検証スクリプトと同じ関数を使う。別々に書くとズレて事故る）。
    from src.betting import candidate_rule as CR
    _cand = config.get("operation", {}).get("candidate_rule") or {}
    CAND_ON = bool(_cand)
    CAND_TYPE = _bt_map.get(_cand.get("bet_type", "2連複"), "nirenfuku")
    CAND_NAME = str(_cand.get("name") or "top1_value")

    # 決着済み・確定済みの買い目はそのまま保持する。
    # 朝の買い目は目安にすぎず、オッズが動けば EV も変わる。日中ずっと
    # 更新され続けると「いつ買えばいいのか」が分からないため、締切が
    # 近づいた時点で一度固定し、以後は触らない（= is_final_pick）。
    keep_bets = [b for b in bets_existing
                 if b.get("is_hit") is not None or b.get("is_final_pick")]
    keep_race_ids = {b["race_id"] for b in keep_bets}

    # race_id → レースメタデータ
    race_meta = {r["id"]: r for r in races_data}

    # 締切まで何分あるかで、更新対象と「今回確定させる対象」を分ける
    FINALIZE_BEFORE_MIN = 20      # 締切20分前を切ったら確定させる
    upcoming_race_ids: list[int] = []
    finalize_race_ids: set[int] = set()
    closed_race_ids: set[int] = set()   # 締切済み＝もう触らないレース
    for race in races_data:
        rid = race["id"]
        if rid in keep_race_ids:
            continue
        ct = race.get("closing_time")
        if not ct:
            # 締切が分からないレースは従来どおり毎回更新する（確定はしない）
            upcoming_race_ids.append(rid)
            continue
        try:
            closing_dt = datetime.strptime(f"{d} {ct}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except Exception:
            upcoming_race_ids.append(rid)
            continue
        minutes_left = (closing_dt - now_jst).total_seconds() / 60
        if minutes_left <= 0:
            # 締切済み。ここで買い目を作り直してはいけない。締切後のオッズは
            # 確定値なので、そこから EV を計算すると「レースが終わってから
            # 買えたはずの買い目」が生える。2026-08-12 は表示された 18 本の
            # うち 5 本がこれで、締切を過ぎてから一覧に載っていた
            # （うち2本は締切の 13 分後と 46 分後）。
            # 確定させ損ねたレースは「買い目なし」が正しい。すでに出ている
            # ものは画面から消さないよう、そのまま凍結して残す。
            closed_race_ids.add(rid)
            continue
        if minutes_left <= FINALIZE_BEFORE_MIN:
            upcoming_race_ids.append(rid)
            finalize_race_ids.add(rid)
        else:
            upcoming_race_ids.append(rid)

    # 締切済みレースの買い目は現状のまま持ち越す（作り直さない）
    kept_keys = {(b["race_id"], b["bet_type"], b["combination"]) for b in keep_bets}
    for b in bets_existing:
        if b["race_id"] not in closed_race_ids:
            continue
        k = (b["race_id"], b["bet_type"], b["combination"])
        if k not in kept_keys:
            keep_bets.append(b)
            kept_keys.add(k)

    logger.info(
        f"refresh_odds: {d}  更新対象={len(upcoming_race_ids)}レース "
        f"（うち今回確定={len(finalize_race_ids)}）/ 保持={len(keep_bets)}件"
    )

    # probs JSONを races 側の race_id に合わせてインデックス化する。
    # ⚠️ race_id で直に突き合わせてはいけない（_sync_bets_from_json と同じ理由）。
    probs_by_race = index_probs_by_race(probs_data, races_data, upcoming_race_ids)

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
            # 単勝と複勝は同じ oddstf ページなので、1回の取得で両方まかなえる
            # （_fetch_raw がキャッシュするため通信は増えない）。
            if "tansho" in needed:
                try:
                    odds_frames.append(sc.get_odds_tansho(stadium_code, d, race_no))
                except Exception as e:
                    logger.warning(f"tansho odds失敗 {stadium_code} R{race_no}: {e}")
            if "fukusho" in needed:
                try:
                    odds_frames.append(sc.get_odds_fukusho(stadium_code, d, race_no))
                except Exception as e:
                    logger.warning(f"fukusho odds失敗 {stadium_code} R{race_no}: {e}")
            # 拡連複は別ページ。⚠️ このページは当日しか出ないので、
            # 日中に取れなかったぶんは二度と手に入らない。
            if "kakurenfuku" in needed:
                try:
                    odds_frames.append(sc.get_odds_kakurenfuku(stadium_code, d, race_no))
                except Exception as e:
                    logger.warning(f"kakurenfuku odds失敗 {stadium_code} R{race_no}: {e}")

        if not odds_frames:
            return race_id, []

        odds_all = pd.concat(odds_frames, ignore_index=True)
        odds_lookup = {
            (row["bet_type"], row["combination"]): row["odds"]
            for _, row in odds_all.iterrows()
        }

        # 市場の含意確率。賭式ごとに 1/オッズ を正規化して控除率を取り除く。
        # 1通りでも欠けると正規化できないので、揃っている賭式だけ作る。
        market: dict[tuple[str, str], float] = {}
        need = {"nirenfuku": 15, "tansho": 6, "sanrenfuku": 20, "sanrentan": 120}
        by_type: dict[str, list] = {}
        for (bt, cb), o in odds_lookup.items():
            if o and not pd.isna(o) and o > 0:
                by_type.setdefault(bt, []).append((cb, float(o)))
        for bt, items in by_type.items():
            if len(items) != need.get(bt):
                continue
            total = sum(1.0 / o for _, o in items)
            for cb, o in items:
                market[(bt, cb)] = (1.0 / o) / total

        candidates = []
        blend_picks = []
        best_of_type: dict[str, dict] = {}   # 賭式ごとの「確率が最大の1点」
        cand_pool: list[dict] = []      # 候補ルールの材料（レース単位で1回判定）
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

            # 検証中の候補ルール（config の operation.candidate_rule）。買わない。
            # 本番ルールと条件は同じで、EV だけを補正後オッズで計算する。
            # 同じ日・同じレースで「板そのまま」と「補正後」を並べて記録し、
            # どちらが当てるかを対で比べるのが目的。
            # market が入っているレースは「全通りに値が出ていて sum(1/odds) が
            # 控除率どおり」＝板として成立している。係数は成立した板だけで
            # 推定したので、薄い板に当ててはいけない。実際 2026-08-24 の
            # 若松1Rは板27.7倍・モデル確率40%で、補正しても EV 4.37 が出た。
            # 候補ルール(top1_value)はレースごとに1点だけ選ぶので、
            # ここでは材料を集めるだけ。判定はループの外で1回行う。
            # market に入っている＝全通りに値が出て sum(1/odds) が控除率
            # どおり＝板として成立している。係数は成立した板で推定したので、
            # 薄い板に当ててはいけない（2026-08-24 若松1Rは板27.7倍・
            # モデル確率40%で、補正しても EV 4.37 が出た）。
            if (CAND_ON and combo["bet_type"] == CAND_TYPE
                    and odds_val > 0 and mp > 0 and market.get(key)):
                cand_pool.append({
                    "bet_type": combo["bet_type"],
                    "combination": combo["combination"],
                    "model_prob": mp,
                    "odds": float(odds_val),
                    "market_prob": market.get(key),
                })

            # 賭式ごとに「確率が最大の1点」だけを残す。
            #
            # 2026-08-30 に賭式を6つへ広げたとき、ここは全組合せを EV 順に
            # 並べて上位5本を取る作りだった。6賭式では上位5本が1つの賭式で
            # 埋まりうるので、**賭式が丸ごと落ちる**。
            # また画面に出す実測回収率（17,090レース・2窓）は「確率が最大の
            # 1点」で測った値なので、選び方を測定と一致させる必要がある。
            #
            # R5（2連複）の挙動は実質変わらない。実測で1レース1本が98.9%
            # （281レース中278）、複数だった3レースも確率最大＝EV最大だった。
            key_bt = combo["bet_type"]
            cur = best_of_type.get(key_bt)
            if cur is None or mp > cur["model_prob"]:
                best_of_type[key_bt] = {
                    "bet_type": key_bt,
                    "combination": combo["combination"],
                    "model_prob": round(mp, 4),
                    "odds": odds_val,
                    "expected_value": round(mp * odds_val, 4),
                }

        # **全賭式の1点を必ず残す。** 賭け金を付けるかどうかだけを分ける。
        #
        # 買う賭式が条件を外した日に行ごと消すと、その日はその賭式が画面から
        # 消えて層の比較ができなくなる。また実測回収率は無条件の1点で測って
        # いるので、記録を条件で間引くと数字の意味がずれる。
        for bt, c in best_of_type.items():
            buy = bt in buy_types
            if buy:
                ov = overrides.get(bt, {})
                lo = ov.get("min_odds", min_odds)
                hi = ov.get("max_odds", max_odds)
                if not (lo <= c["odds"] <= hi):
                    buy = False
                elif ov.get("min_model_prob") is not None and c["model_prob"] < ov["min_model_prob"]:
                    buy = False
                elif ov.get("max_model_prob") is not None and c["model_prob"] > ov["max_model_prob"]:
                    buy = False
                elif c["expected_value"] < ov.get("min_ev", min_ev):
                    buy = False
            candidates.append({**c, "_buy": buy})

        # 候補ルール(top1_value)をレースに1回だけ当てる。
        # ①確率が最大の1点を選ぶ（オッズを見ない）②板から確定オッズを見込む
        # ③見込みで期待値が閾値を超えたら記録する。判定の中身は
        # src/betting/candidate_rule.py（検証スクリプトと共有）。
        if CAND_ON and cand_pool:
            got = CR.evaluate(cand_pool, _cand)
            if got:
                mk = next((c.get("market_prob") for c in cand_pool
                           if c["combination"] == got["combination"]), None)
                got["market_prob"] = round(mk, 4) if mk else None
                blend_picks.append(got)

        # 締切間際の板をまるごと残す。ここでしか取れない。
        #
        # EV は観測オッズで計算しているが、pari-mutuel なので払戻は確定プールで
        # 決まる。観測値は確定値の推定でしかなく、極端なほど行き過ぎている
        # （2026-08-21 実測: log(確定)=1.671+0.486*log(朝の板)、傾きが0.5弱）。
        # EV で選ぶ＝オッズが高いものを選ぶ以上、構造的に「これから下がる側」を
        # 拾う。縮小してから EV を計算すれば直せるはずだが、その係数を推定するには
        # 「締切前の板」と「確定オッズ」の対データが要る。
        #
        # 朝の板では足りない（R^2=0.12、確定オッズをほとんど説明できない）。
        # 必要なのは実際に買い目を固定する時点＝締切間際の板で、そこは今まで
        # ルールが選んだ組合せしか残っていなかった（選択バイアス付きで84件）。
        # ここでは全組合せを取得済みなので、捨てずに残せばよい。
        board = [{"bet_type": bt, "combination": cb, "odds": float(o)}
                 for (bt, cb), o in odds_lookup.items()
                 if o and not pd.isna(o) and o > 0]
        return race_id, candidates, blend_picks, board

    new_bets: list[dict] = []
    boards: dict[int, list] = {}      # 締切間際の板（race_id -> 全組合せ）
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_race_odds, rid): rid for rid in probs_by_race}
        for future in as_completed(futures):
            rid = futures[future]
            try:
                race_id, race_bets, race_blend, race_board = future.result()
                if race_id in finalize_race_ids and race_board:
                    boards[race_id] = race_board
                meta = race_meta.get(race_id, {})
                common = {
                    "bet_id": None,
                    "race_id": race_id,
                    "stadium_name": meta.get("stadium", ""),
                    "race_no": meta.get("race_no"),
                    "grade": meta.get("grade"),
                    "race_type": meta.get("race_type"),
                    "closing_time": meta.get("closing_time"),
                    "is_night": meta.get("is_night"),
                    "is_hit": None,
                    "actual_payout": None,
                    # 締切間近で固定した「これを買えばよい」買い目
                    "is_final_pick": race_id in finalize_race_ids,
                }
                for b in race_bets:
                    # ⚠️ 賭け金を付けるのは「買う賭式かつ条件を満たした1点」だけ。
                    # 2026-08-30 に賭式を6つへ増やしたとき、ここが全賭式に
                    # fixed_amount を付ける作りのままで、記録だけのはずの
                    # 3連単などにも500円が付いていた（画面上「買え」に見える）。
                    is_buy = bool(b.pop("_buy", False))
                    new_bets.append({**common, **b,
                                     "recommended_amount": fixed_amount if is_buy else 0,
                                     "rule": "r5" if is_buy else "record"})
                for b in race_blend:
                    # 検証中の候補。賭け金 0 で、画面でも別扱いにする。
                    new_bets.append({**common, **b,
                                     "recommended_amount": 0,
                                     "rule": CAND_NAME})
            except Exception as e:
                logger.warning(f"race_id={rid} オッズ更新失敗: {e}")

    new_bets.sort(key=lambda b: (b.get("race_no") or 0, -(b.get("expected_value") or 0)))
    all_bets = keep_bets + new_bets
    n_final = sum(1 for b in all_bets if b.get("is_final_pick"))
    n_blend = sum(1 for b in all_bets if b.get("rule") in CANDIDATE_RULES)
    bets_path.write_text(json.dumps(all_bets, ensure_ascii=False, indent=None), encoding="utf-8")

    # 締切間際の板を日付ごとのファイルに貯める。1レース1回だけ書き、
    # 既にあるレースは上書きしない（最初に確定した時点の板が「買える値」）。
    # 15分ごとの全スナップショットを残すとリポジトリが太るので、確定した
    # レースだけに絞る。用途は observed→settled の縮小係数の推定。
    if boards:
        board_path = docs_data / f"board_{d}.json.gz"
        existing: dict = {}
        if board_path.exists():
            try:
                existing = json.loads(gzip.decompress(board_path.read_bytes()).decode("utf-8"))
            except Exception as e:
                logger.warning(f"board 読み込み失敗（作り直します）: {e}")
                existing = {}
        races_in = existing.setdefault("races", {})
        added = 0
        for rid, rows in boards.items():
            if str(rid) in races_in:
                continue
            meta = race_meta.get(rid, {})
            races_in[str(rid)] = {
                "stadium": meta.get("stadium", ""),
                "race_no": meta.get("race_no"),
                "closing_time": meta.get("closing_time"),
                "captured_at": now_jst.isoformat(),
                "odds": rows,
            }
            added += 1
        if added:
            existing["date"] = str(d)
            blob = json.dumps(existing, ensure_ascii=False).encode("utf-8")
            board_path.write_bytes(gzip.compress(blob, 9))
            logger.info(f"締切前の板を保存: {board_path.name} "
                        f"(+{added}レース / 累計{len(races_in)}レース / "
                        f"{board_path.stat().st_size/1e6:.2f} MB)")

    logger.info(
        f"refresh_odds完了: 保持={len(keep_bets)}件 更新={len(new_bets)}件 "
        f"（確定済み合計={n_final}件 / 候補ルール={n_blend}件）"
    )

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


def cmd_update(target_date: date | None = None, max_workers: int = 5,
               predict: bool = True, skip_odds: bool = False):
    """履歴DBをその日のデータで埋める。

    predict=False / skip_odds=True は、朝の買い目をクラウド(predict_cloud)が
    作るようになってからのローカル用。クラウドが
      - 買い目（bets/probs/races JSON）
      - その時点の板（odds_raw_<日付>.json.gz）
    を push しているので、ローカルが同じものを取り直す必要はない。
    ローカルでも予測すると、クラウドが出した買い目を別のオッズで作り直して
    上書きしてしまう。オッズを取り直すと、買い目を選んだ時点の板が
    あとの時刻の板で潰れる。どちらも記録を壊すので、やらない。

    残るローカルの仕事は「履歴DBを埋める」ことだけで、これは時刻に依存しない。
    """
    import subprocess
    keep_awake()
    d = target_date or date.today()
    logger.info(f"=== UPDATE 開始: {d} (予測={'する' if predict else 'しない'}"
                f" / オッズ={'取らない' if skip_odds else '取る'}) ===")
    cmd_collect(d, max_workers=max_workers, skip_before_info=True,
                skip_odds=skip_odds)
    if predict:
        # 今日の買い目を先に作る。過去日の穴埋めより優先する。
        # キャッチアップを収集の中でやっていたため、2026-08-14 は 8/13 の
        # 取り直し（約1,100リクエスト）が終わるまで当日の予測が始まらず、
        # レースが始まっても買い目が出なかった。過去日は急がない。
        cmd_predict(d)
    logger.info(f"=== UPDATE 完了: {d} ===")

    if target_date is None or target_date == date.today():
        _catchup_missed_results(max_workers=max_workers)

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

    ⚠️ ここは**ローカルが落ちた日だけ**動く予備経路（odds_archive.yml が
    bets_<日付>.json の有無で判断する）。通常日の退避は cmd_cloud_predict が
    collect_day の結果から書いており、そちらには単勝・複勝も入る。
    この予備経路は3ページ/レースに絞ってある（55分の制限があるため）。
    単勝・複勝・拡連複は落ちた日には取れない。増やすなら所要時間を先に測ること。
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
                            row = {
                                "stadium_code": str(r["stadium_code"]),
                                "race_no": int(r["race_no"]),
                                "bet_type": str(r["bet_type"]),
                                "combination": str(r["combination"]),
                                "odds": float(r["odds"]),
                            }
                            up = r.get("odds_upper")
                            if up is not None and up == up:
                                row["odds_upper"] = float(up)
                            rows.append(row)
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
    # 退避JSONはその日に取った「板」であって精算値ではない。取り込むのは
    # 後日なので日付からは live と判定されない。明示して板として入れる。
    n = save_odds(df, is_final=False, force_live=True)
    logger.info(f"オッズ取込完了: {d} {n} 件（当日の板として取り込み）")


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
        # --no-predict / --no-odds: 買い目と板はクラウドが作るので、
        # ローカルは履歴DBを埋めるだけにする（cmd_update の説明を参照）
        cmd_update(d, max_workers=_workers_arg(args),
                   predict="--no-predict" not in args,
                   skip_odds="--no-odds" in args)
    elif cmd == "collect":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_collect(d, max_workers=_workers_arg(args))
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
    elif cmd == "predict_cloud":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_predict_cloud(d)
    elif cmd == "collect_results":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        cmd_collect_results(d)
    elif cmd == "refresh_odds":
        d = date.fromisoformat(args[1]) if len(args) > 1 else None
        # update / collect と同じ読み方にそろえる。位置引数で int() すると
        # フラグを足した日に ValueError で落ちる（2026-08-22〜23 の更新停止）。
        cmd_refresh_odds(d, max_workers=_workers_arg(args))
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
