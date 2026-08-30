"""終わったレースの確定オッズを取りに行く。

日次の仕組みは確定オッズを DB に入れていない。朝の update が取るのは
発売直後の薄いオッズで、夜の collect_results は結果と払戻しか取らない
（racelist/odds は取得しない設計）。そのため 5月以降 15,800 レースの
うち、2連複15通りが揃っているのは 4,267 レース（27%）しかない。

検証は市場確率を必要とし、市場確率は 1/オッズ を全通り正規化して作る。
1通りでも欠けると計算できないので、揃っていないレースは丸ごと使えない。

確定オッズはレース後も公開されている（2026-08-13 に実地確認）。
「オッズは遡及取得不可」は発売中の途中経過の話で、確定値は残る。

既に揃っているレースは飛ばすので、途中で止めて再実行してよい。

使い方:
    python scripts/backfill_final_odds.py <from> <to> [--bet-type nirenfuku]
                                          [--workers 5] [--max-minutes 60]
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text                       # noqa: E402
from src.ingestion.database import get_engine, init_db   # noqa: E402
from src.ingestion.saver import save_odds         # noqa: E402
from src.scraping.official import BoatRaceScraper  # noqa: E402
from src.utils.helpers import load_config         # noqa: E402
from src.utils.logger import get_logger           # noqa: E402

logger = get_logger(__name__)

# 賭式ごとの (取得メソッド名, 揃うべき通り数)
#
# ⚠️ kakurenfuku だけ**当日しか取れない**。他の賭式はレース後も確定値が
# 公開され続けるので後日でも回収できるが、oddsk ページは翌日以降
# テーブルごと消える（2026-08-30 実測: 7日前・7週間前とも 0件）。
# つまり **その日のうちに取らないと永久に失われる**。
# 夜間の collect_results から呼ばれる分は同日なので間に合う。
KINDS = {
    "nirenfuku": ("get_odds_nirenfuku", 15),
    "tansho": ("get_odds_tansho", 6),
    "sanrenfuku": ("get_odds_sanrenfuku", 20),
    "sanrentan": ("get_odds_sanrentan", 120),
    "kakurenfuku": ("get_odds_kakurenfuku", 15),
}

TARGETS = """
    SELECT r.id, s.code, r.race_no, r.race_date
      FROM races r
      JOIN stadiums s ON s.id = r.stadium_id
     WHERE r.race_date BETWEEN :d1 AND :d2
       AND EXISTS (SELECT 1 FROM race_results rr WHERE rr.race_id = r.id)
       AND (SELECT COUNT(*) FROM odds o
             WHERE o.race_id = r.id AND o.bet_type = :bt
               AND o.is_final = 1 AND o.odds > 0) < :need
     ORDER BY r.race_date DESC, s.code, r.race_no
"""


def main() -> None:
    d1, d2 = sys.argv[1], sys.argv[2]
    bt = sys.argv[sys.argv.index("--bet-type") + 1] if "--bet-type" in sys.argv else "nirenfuku"
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 5
    max_min = float(sys.argv[sys.argv.index("--max-minutes") + 1]) if "--max-minutes" in sys.argv else 60
    method, need = KINDS[bt]

    cfg = load_config()
    init_db(cfg)
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(TARGETS),
                            {"d1": d1, "d2": d2, "bt": bt, "need": need}).fetchall()
    logger.info(f"確定オッズ backfill: {bt} {d1}〜{d2}  対象 {len(rows):,}レース "
                f"(揃っていないもののみ)")
    if not rows:
        return

    t0 = time.time()
    done = saved = failed = 0
    with BoatRaceScraper(cfg) as scraper:
        fn = getattr(scraper, method)

        def fetch(row):
            _rid, code, rno, rdate = row
            return fn(str(code), pd.to_datetime(rdate).date(), int(rno))

        # 期限で打ち切れるよう、まとめて投げずに小分けにする
        chunk = workers * 20
        for i in range(0, len(rows), chunk):
            if (time.time() - t0) / 60 >= max_min:
                logger.info(f"時間切れ ({max_min:.0f}分) — ここまでで中断")
                break
            batch = rows[i:i + chunk]
            frames = []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(fetch, r): r for r in batch}
                for fut in as_completed(futs):
                    done += 1
                    try:
                        df = fut.result()
                        if df is not None and len(df):
                            frames.append(df)
                    except Exception as e:
                        failed += 1
                        logger.warning(f"取得失敗 {futs[fut][1]} {futs[fut][2]}R: {e}")
            if frames:
                saved += save_odds(pd.concat(frames, ignore_index=True), is_final=True)
            logger.info(f"  {done:,}/{len(rows):,} レース  保存 {saved:,}行  "
                        f"失敗 {failed}  経過 {(time.time() - t0) / 60:.1f}分")

    logger.info(f"完了: {done:,}レース処理 / {saved:,}行保存 / 失敗 {failed}")


if __name__ == "__main__":
    main()
