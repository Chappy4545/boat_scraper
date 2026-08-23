"""DBから静的JSONファイルを生成し docs/data/ に出力する。
毎日の predict 後に実行し、GitHub Pages用データを更新する。
"""
import json
from datetime import date
from pathlib import Path

from sqlalchemy import func as sa_func, or_ as sa_or

# 検証中の候補ルールの見送り理由（main.CANDIDATE_REASON と同じ値）。
# ここから main を import すると循環するので、定数を持つ。
CANDIDATE_REASON = "候補ルール(混合)"

from src.ingestion.database import get_session, get_engine
from src.ingestion.models import (
    Race, RaceEntry, Prediction, Bet, Stadium, BacktestResult, RaceResult,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

DOCS_DIR = Path(__file__).parent.parent / "docs"
DATA_DIR = DOCS_DIR / "data"

# 損益に数えてよい買い目の条件。集計する箇所すべてで同じものを使う。
#
# date(created_at) <= race_date が要る理由:
# _catchup_missed_results は取り逃した日に cmd_predict を走らせ直すため、
# レースが終わったあとの確定オッズで買い目が生成されることがある。これは
# 当日買えなかった買い目なので、混ぜると「賭けていない金」が損益に乗る。
# 2026-08-13 時点で直近7日に 516 本・47万円ぶんが紛れ込み、実際は 55 本
# なのに「1,189件・-358,310円」と表示されていた。
BOUGHT = ("b.is_pass = 0 AND b.is_hit IS NOT NULL "
          "AND date(b.created_at) <= r.race_date")


def live_since() -> str:
    """現行ルールでの運用開始日。収支はこの日以降だけを集計する。

    それ以前は別のルール・別のモデルで出した買い目なので、混ぜると
    今のルールの成績が読めない。2026-08-10 だけで 357 本・-130,420 円あり、
    直近7日の数字がその日にほぼ決まってしまっていた。
    """
    from src.utils.helpers import load_config
    try:
        return str(load_config().get("operation", {}).get("live_since") or "1970-01-01")
    except Exception:
        return "1970-01-01"


def paper_mode() -> bool:
    """検証モードか。true の間は買い目を出すだけで実際には賭けない。

    買い目の中身も記録も判定も変わらない。変わるのは画面の見せ方だけで、
    表示される損益は「賭けていたらこうなった」という仮の数字になる。
    """
    from src.utils.helpers import load_config
    try:
        return bool(load_config().get("operation", {}).get("paper_mode", False))
    except Exception:
        return False


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

        # 着順（1着から順の艇番）。races / bets どちらにも載せる。
        # 買い目が無いレースでも結果は見たいので、race_ids 全体で引く。
        order_map: dict[int, list[int]] = {}
        if race_ids:
            for rid, _order, boat in (
                session.query(RaceResult.race_id, RaceResult.arrival_order, RaceResult.boat_no)
                .filter(RaceResult.race_id.in_(race_ids))
                .order_by(RaceResult.race_id, RaceResult.arrival_order)
                .all()
            ):
                if boat is not None:
                    order_map.setdefault(rid, []).append(int(boat))

        # races JSON
        races_json = []
        for r, s in races:
            races_json.append({
                "result_order": order_map.get(r.id),
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
        #
        # 買った買い目に加えて、検証中の候補ルール（賭け金0・is_pass=1）も出す。
        # 出さないと、判定のたびにこの JSON が作り直され、クラウドが日中に
        # 記録した候補が消える。2026-08-23 実測: 判定を回すと 44件(候補11) が
        # 29件(候補0) に縮んでいた。候補はここにしか残らない日もある。
        # 画面側は rule/recommended_amount で除外するので混ざらない
        # （docs/js/app.js の isCandidate）。
        bets_raw = (
            session.query(Bet, Race, Stadium)
            .join(Race, Bet.race_id == Race.id)
            .join(Stadium, Race.stadium_id == Stadium.id)
            # レース後に生成された買い目は当日買えなかったので出さない（BOUGHT 参照）
            .filter(Race.race_date == d,
                    sa_or(Bet.is_pass == False,
                          Bet.pass_reason == CANDIDATE_REASON),
                    sa_func.date(Bet.created_at) <= Race.race_date)
            .order_by(Race.race_no, Bet.expected_value.desc())
            .all()
        )

        # 着順は上で race_ids 全体から order_map を作ってあるのでそれを使う
        # （judge_live も同じキー result_order で書き込む）
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
                "result_order": order_map.get(b.race_id),
                "is_final_pick": bool(b.is_final_pick),
                # 候補ルールの行だと分かるようにする。画面側の除外条件であり、
                # クラウドが書く JSON と同じ形にしておく。
                **({"rule": "market_blend"}
                   if b.pass_reason == CANDIDATE_REASON else {}),
            }
            for b, r, s in bets_raw
        ]

    date_str = str(d)
    races_path = DATA_DIR / f"races_{date_str}.json"
    bets_path = DATA_DIR / f"bets_{date_str}.json"
    races_path.write_text(json.dumps(races_json, ensure_ascii=False, indent=None), encoding="utf-8")

    # 中身のある記録を空で潰さない。
    # 2026-08-17 のキャッチアップが 08-16 を再予測し、その買い目が翌日づけに
    # なった結果 BOUGHT の条件から外れ、ここが 0 件を書いて「16本・的中3本」
    # という実績が消えた。DB 側の都合で消えてよい記録ではない。
    if not bets_json and bets_path.exists():
        try:
            existing = json.loads(bets_path.read_text(encoding="utf-8"))
        except Exception:
            existing = []
        if existing:
            logger.warning(
                f"export: {bets_path.name} は 0 件になるため書き換えません"
                f"（既存 {len(existing)}件を保持）")
            return {"races": len(races_json), "bets": len(existing)}

    bets_path.write_text(json.dumps(bets_json, ensure_ascii=False, indent=None), encoding="utf-8")
    logger.info(f"export: {races_path.name} ({len(races_json)}件), {bets_path.name} ({len(bets_json)}件)")
    return {"races": len(races_json), "bets": len(bets_json)}


def export_meta(source: str = "local") -> None:
    """docs/data/meta.json にオッズ最終更新時刻を書き込む。"""
    _ensure_data_dir()
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    now_jst = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    path = DATA_DIR / "meta.json"
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, ValueError):
        existing = {}
    existing["last_refreshed"] = now_jst
    existing["source"] = source
    # 検証モードかどうかを画面に伝える。買い目の中身は変わらないので、
    # これが無いと「賭けるつもりの買い目」と見分けがつかない。
    existing["paper_mode"] = paper_mode()
    existing["live_since"] = live_since()
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=None), encoding="utf-8")
    logger.info(f"export: meta.json (last_refreshed={now_jst}, "
                f"paper_mode={existing['paper_mode']})")


def export_stadiums() -> None:
    """場マスタを docs/data/stadiums.json に出す。

    クラウドで当日予測を回すとき、385MB の履歴DBは持ち込まない。だが特徴量には
    場別コース成績（1〜6コースの勝率・2連率・3連率）が要る。24行・15KB しか
    ないので、リポジトリに置いて持ち回る。中身が変わることは滅多にない。
    """
    _ensure_data_dir()
    from src.ingestion.database import get_engine
    from sqlalchemy import text as sa_text

    with get_engine().connect() as conn:
        cols = [c[1] for c in conn.execute(sa_text("PRAGMA table_info(stadiums)"))]
        rows = [dict(zip(cols, r)) for r in conn.execute(sa_text("SELECT * FROM stadiums"))]
    if not rows:
        logger.warning("export: stadiums が空のため書き出しません")
        return
    path = DATA_DIR / "stadiums.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    logger.info(f"export: {path.name} ({len(rows)}場)")


def export_probs(target_date: date) -> None:
    """当日の全組み合わせ+model_probをdocs/data/probs_YYYY-MM-DD.jsonに保存する。
    GitHub Actionsのrefresh_oddsがDBなしでEV再計算するために使う。
    """
    _ensure_data_dir()
    from collections import defaultdict
    from src.ingestion.database import get_engine
    from sqlalchemy import text as sa_text

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sa_text("""
            SELECT b.race_id, s.code AS stadium_code, r.race_no, r.closing_time,
                   b.bet_type, b.combination, b.model_prob
            FROM bets b
            JOIN races r ON b.race_id = r.id
            JOIN stadiums s ON r.stadium_id = s.id
            WHERE r.race_date = :d AND b.model_prob IS NOT NULL
            ORDER BY s.code, r.race_no, b.bet_type, b.combination
        """), {"d": str(target_date)}).fetchall()

    race_map: dict = defaultdict(lambda: {
        "race_id": None, "stadium_code": None, "race_no": None,
        "closing_time": None, "combinations": []
    })
    total = 0
    for race_id, stadium_code, race_no, closing_time, bet_type, combination, model_prob in rows:
        entry = race_map[race_id]
        entry["race_id"] = race_id
        entry["stadium_code"] = stadium_code
        entry["race_no"] = race_no
        entry["closing_time"] = closing_time
        entry["combinations"].append({
            "bet_type": bet_type,
            "combination": combination,
            "model_prob": round(model_prob, 6),
        })
        total += 1

    data = {"date": str(target_date), "races": list(race_map.values())}
    path = DATA_DIR / f"probs_{target_date}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=None), encoding="utf-8")
    logger.info(f"export: {path.name} ({len(race_map)}レース, {total}組み合わせ)")


def export_performance() -> None:
    """現行ルールでの収支サマリー＋日別実績を docs/data/performance.json に保存する。"""
    _ensure_data_dir()
    _ls = live_since()
    from src.ingestion.database import get_engine
    from sqlalchemy import text as sa_text

    with get_session() as session:
        # レース後に生成された買い目を除く（BOUGHT と同じ条件）。
        # これを入れないと全期間の収支に「賭けていない金」が乗る。
        all_bets = (
            session.query(Bet)
            .join(Race, Bet.race_id == Race.id)
            .filter(Bet.is_pass == False,
                    sa_func.date(Bet.created_at) <= Race.race_date,
                    Race.race_date >= _ls)
            .all()
        )
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
        rows = conn.execute(sa_text(f"""
            SELECT r.race_date,
                   COUNT(*) AS total_bets,
                   SUM(CASE WHEN b.is_hit = 1 THEN 1 ELSE 0 END) AS hits,
                   SUM(b.recommended_amount) AS invested,
                   SUM(CASE WHEN b.is_hit = 1 THEN CAST(b.recommended_amount * b.actual_payout / 100 AS INTEGER) ELSE 0 END) AS returned
            FROM bets b
            JOIN races r ON b.race_id = r.id
            WHERE {BOUGHT} AND r.race_date >= '{_ls}'
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


def export_pdca() -> None:
    """PDCA判断用の集計をdocs/data/pdca.jsonに出力する。
    - windows: 7d/30d/all の総合ROI + bet_type別
    - band_hit_rates: model_prob帯 × bet_type の実測hit率とROI (直近30日)
    - calibration_recheck: config.calibration_table_pl と実測の乖離
    - daily: 日次実績を bet_type別まで分解 (直近90日)

    いずれも現行ルールの運用開始日以降だけを対象にする（live_since）。
    """
    _ls = live_since()
    _ensure_data_dir()
    from datetime import datetime, timezone, timedelta
    from src.ingestion.database import get_engine
    from src.utils.helpers import load_config
    from sqlalchemy import text as sa_text

    JST = timezone(timedelta(hours=9))
    now_jst = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    config = load_config()

    engine = get_engine()

    def _agg(rows):
        """rows: iterable of (bets, hits, invested, returned) → dict"""
        bets, hits, invested, returned = 0, 0, 0, 0
        for r in rows:
            bets += r[0] or 0
            hits += r[1] or 0
            invested += r[2] or 0
            returned += r[3] or 0
        return {
            "bets": bets, "hits": hits, "invested": invested, "returned": returned,
            "roi": round(returned / invested, 4) if invested else None,
            "hit_rate": round(hits / bets, 4) if bets else None,
            "profit": returned - invested,
        }

    def _window_query(days: int | None):
        """days=None のときは全期間"""
        where_date = "" if days is None else f"AND r.race_date >= date('now','-{days} days')"
        sql = f"""
            SELECT b.bet_type,
                   COUNT(*) AS bets,
                   SUM(CASE WHEN b.is_hit=1 THEN 1 ELSE 0 END) AS hits,
                   SUM(b.recommended_amount) AS invested,
                   SUM(CASE WHEN b.is_hit=1 THEN CAST(b.recommended_amount * b.actual_payout / 100 AS INTEGER) ELSE 0 END) AS returned
            FROM bets b JOIN races r ON b.race_id=r.id
            WHERE {BOUGHT} AND r.race_date >= '{_ls}' {where_date}
            GROUP BY b.bet_type
        """
        with engine.connect() as conn:
            return conn.execute(sa_text(sql)).fetchall()

    def _window(days):
        rows = _window_query(days)
        by_bet_type = {r[0]: _agg([(r[1], r[2], r[3], r[4])]) for r in rows}
        total = _agg([(r[1], r[2], r[3], r[4]) for r in rows])
        return {"total": total, "by_bet_type": by_bet_type}

    windows = {
        "7d": _window(7),
        "30d": _window(30),
        "all": _window(None),
    }

    # band_hit_rates (直近30日)
    band_sql = f"""
        SELECT b.bet_type,
               CASE
                 WHEN b.model_prob < 0.03 THEN 1
                 WHEN b.model_prob < 0.05 THEN 2
                 WHEN b.model_prob < 0.07 THEN 3
                 WHEN b.model_prob < 0.10 THEN 4
                 WHEN b.model_prob < 0.15 THEN 5
                 WHEN b.model_prob < 0.20 THEN 6
                 WHEN b.model_prob < 0.30 THEN 7
                 WHEN b.model_prob < 0.50 THEN 8
                 ELSE 9
               END AS band_idx,
               COUNT(*) AS n,
               SUM(CASE WHEN b.is_hit=1 THEN 1 ELSE 0 END) AS hits,
               AVG(b.odds) AS avg_odds,
               AVG(b.model_prob) AS avg_mp,
               SUM(b.recommended_amount) AS invested,
               SUM(CASE WHEN b.is_hit=1 THEN CAST(b.recommended_amount * b.actual_payout / 100 AS INTEGER) ELSE 0 END) AS returned
        FROM bets b JOIN races r ON b.race_id=r.id
        WHERE {BOUGHT} AND r.race_date >= '{_ls}' AND r.race_date >= date('now','-30 days')
        GROUP BY b.bet_type, band_idx ORDER BY b.bet_type, band_idx
    """
    band_labels = {1:"0-3%",2:"3-5%",3:"5-7%",4:"7-10%",5:"10-15%",6:"15-20%",7:"20-30%",8:"30-50%",9:"50%+"}
    with engine.connect() as conn:
        band_rows = conn.execute(sa_text(band_sql)).fetchall()
    band_hit_rates = []
    for r in band_rows:
        n, hits = r[2], r[3]
        invested, returned = r[6] or 0, r[7] or 0
        band_hit_rates.append({
            "bet_type": r[0],
            "band": band_labels[r[1]],
            "band_idx": r[1],
            "n": n,
            "hits": hits,
            "hit_rate": round(hits / n, 4) if n else None,
            "avg_odds": round(r[4], 2) if r[4] else None,
            "avg_model_prob": round(r[5], 4) if r[5] else None,
            "roi": round(returned / invested, 4) if invested else None,
        })

    # calibration_recheck: config の calibration_table_pl 各行と実測を比較
    # 実装: 設定の hit_rate (=calibrated model_prob 値) と一致する bet を抽出して実測hit率を出す
    overrides = config.get("betting", {}).get("bet_type_overrides", {})
    calibration_recheck = []
    use_pl = config.get("model", {}).get("use_ranker", False)
    for bt, ov in overrides.items():
        table = None
        if use_pl:
            table = ov.get("calibration_table_pl") or ov.get("calibration_table")
        else:
            table = ov.get("calibration_table")
        if not table:
            continue
        for i, entry in enumerate(table):
            target = round(entry["hit_rate"], 6)
            # calibrated値がtargetに近いbetを集計 (許容誤差0.0005)
            sql = f"""
                SELECT COUNT(*), SUM(CASE WHEN b.is_hit=1 THEN 1 ELSE 0 END),
                       SUM(b.recommended_amount),
                       SUM(CASE WHEN b.is_hit=1 THEN CAST(b.recommended_amount * b.actual_payout / 100 AS INTEGER) ELSE 0 END)
                FROM bets b JOIN races r ON b.race_id=r.id
                WHERE {BOUGHT} AND r.race_date >= '{_ls}'
                  AND b.bet_type=:bt
                  AND ABS(b.model_prob - :tgt) < 0.0005
                  AND r.race_date >= date('now','-30 days')
            """
            with engine.connect() as conn:
                row = conn.execute(sa_text(sql), {"bt": bt, "tgt": target}).fetchone()
            n = row[0] or 0
            hits = row[1] or 0
            invested = row[2] or 0
            returned = row[3] or 0
            actual = hits / n if n else None
            delta_pct = round(100 * (actual - entry["hit_rate"]) / entry["hit_rate"], 1) if actual is not None else None
            calibration_recheck.append({
                "bet_type": bt,
                "row_idx": i,
                "raw_mp_max": entry.get("raw_mp_max"),
                "config_hit_rate": entry["hit_rate"],
                "actual_n": n,
                "actual_hits": hits,
                "actual_hit_rate": round(actual, 4) if actual is not None else None,
                "delta_pct": delta_pct,
                "actual_roi": round(returned / invested, 4) if invested else None,
            })

    # daily × bet_type (直近90日)
    daily_sql = f"""
        SELECT r.race_date, b.bet_type,
               COUNT(*) AS bets,
               SUM(CASE WHEN b.is_hit=1 THEN 1 ELSE 0 END) AS hits,
               SUM(b.recommended_amount) AS invested,
               SUM(CASE WHEN b.is_hit=1 THEN CAST(b.recommended_amount * b.actual_payout / 100 AS INTEGER) ELSE 0 END) AS returned
        FROM bets b JOIN races r ON b.race_id=r.id
        -- is_hit IS NOT NULL が抜けていたため、未判定の買い目が「外れ」として
        -- 集計されていた。2026-08-11 時点で 8/1以降が全て未判定だったため、
        -- 日次・累積損益が -500万円という実在しない数字になっていた。
        -- 他の集計(windows / band_hit_rates)には元から入っている条件。
        WHERE {BOUGHT} AND r.race_date >= '{_ls}'
        GROUP BY r.race_date, b.bet_type
        HAVING r.race_date >= date('now','-90 days')
        ORDER BY r.race_date DESC, b.bet_type
    """
    with engine.connect() as conn:
        daily_rows = conn.execute(sa_text(daily_sql)).fetchall()
    daily_map: dict = {}
    for r in daily_rows:
        d_str = str(r[0])
        entry = daily_map.setdefault(d_str, {"date": d_str, "total": _agg([]), "by_bet_type": {}})
        agg = _agg([(r[2], r[3], r[4], r[5])])
        entry["by_bet_type"][r[1]] = agg
    for d_str, entry in daily_map.items():
        rows = [(v["bets"], v["hits"], v["invested"], v["returned"]) for v in entry["by_bet_type"].values()]
        entry["total"] = _agg(rows)
    daily = sorted(daily_map.values(), key=lambda x: x["date"], reverse=True)

    pdca = {
        "generated_at": now_jst,
        "use_pl": use_pl,
        "windows": windows,
        "band_hit_rates": band_hit_rates,
        "calibration_recheck": calibration_recheck,
        "daily": daily,
    }
    path = DATA_DIR / "pdca.json"
    path.write_text(json.dumps(pdca, ensure_ascii=False, indent=None), encoding="utf-8")
    logger.info(f"export: {path.name} (windows=3, bands={len(band_hit_rates)}, recheck={len(calibration_recheck)}, daily={len(daily)})")
