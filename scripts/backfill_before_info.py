"""欠けている直前情報（展示タイム・気象）を後から埋める。

なぜ必要か
----------
直前情報の収集は**2回止まっている**。

    2026-05-21  一度止まる（推論時に中央値で埋まる列になっていた）
    2026-06-10  直したはず
    2026-09-03  同じ形でまた止まっていた:
                  01〜04月 100% / 05月 74% / 06〜07月 100%
                  08月 33% / 09月 **0%**

原因は「どの収集経路も集めていなかった」こと。朝の一括収集は
`skip_before_info=True` が既定（レース20〜30分前公開なので朝はまだ無く、
これは正しい）、夜の `collect_day_results` も集めていなかった。
2026-09-03 に夜の経路へ組み込み、`daily_check` に充足率の見張りを置いた。

⚠️ 直前情報は**レース後も取得できる**（2026-09-03 に 08-20 / 09-01 / 09-02 で
実地確認）。だから欠けても永久喪失ではない。オッズとはここが違う。

既に入っているレースは飛ばすので、途中で止めて再実行してよい。

使い方:
    python scripts/backfill_before_info.py 2026-08-01 2026-09-03
    python scripts/backfill_before_info.py 2026-08-01 2026-09-03 --workers 5 --max-minutes 30
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text                              # noqa: E402
from src.ingestion.database import get_engine, init_db   # noqa: E402
from src.ingestion.saver import save_before_info, save_weather  # noqa: E402
from src.scraping.official import BoatRaceScraper        # noqa: E402
from src.utils.helpers import load_config                # noqa: E402
from src.utils.logger import get_logger                  # noqa: E402

logger = get_logger(__name__)

# 直前情報が1行も無いレースだけを対象にする。
# ⚠️ 中止レースは永久に取れないので、何度回しても残る。件数だけ見て
#    「まだ終わっていない」と誤解しないこと。
TARGETS = """
    SELECT r.id, s.code, r.race_no, r.race_date
      FROM races r
      JOIN stadiums s ON s.id = r.stadium_id
     WHERE r.race_date BETWEEN :d1 AND :d2
       AND NOT EXISTS (SELECT 1 FROM before_info b WHERE b.race_id = r.id)
     ORDER BY r.race_date DESC, s.code, r.race_no
"""


def main() -> None:
    d1, d2 = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 5
    max_min = float(sys.argv[sys.argv.index("--max-minutes") + 1]) if "--max-minutes" in sys.argv else 60

    cfg = load_config()
    init_db(cfg)
    with get_engine().connect() as conn:
        rows = conn.execute(text(TARGETS), {"d1": d1, "d2": d2}).fetchall()
    logger.info(f"直前情報 backfill: {d1}〜{d2}  対象 {len(rows):,}レース（欠けているものだけ）")
    if not rows:
        return

    t0 = time.time()
    done = saved_bi = saved_wx = empty = 0
    with BoatRaceScraper(cfg) as scraper:
        def fetch(row):
            _rid, code, rno, rdate = row
            return scraper.get_before_info_and_weather(
                str(code), pd.to_datetime(rdate).date(), int(rno))

        chunk = workers * 20
        for i in range(0, len(rows), chunk):
            if (time.time() - t0) / 60 >= max_min:
                logger.info(f"時間切れ ({max_min:.0f}分) — ここまでで中断")
                break
            batch = rows[i:i + chunk]
            bis, wxs = [], []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(fetch, r): r for r in batch}
                for fut in as_completed(futs):
                    done += 1
                    try:
                        bi, wx = fut.result()
                        if bi is not None and not bi.empty:
                            bis.append(bi)
                        else:
                            empty += 1
                        if wx is not None and not wx.empty:
                            wxs.append(wx)
                    except Exception as e:
                        logger.warning(f"取得失敗: {str(e)[:60]}")
            if bis:
                saved_bi += save_before_info(pd.concat(bis, ignore_index=True))
            if wxs:
                saved_wx += save_weather(pd.concat(wxs, ignore_index=True))
            logger.info(f"  {done:,}/{len(rows):,}  直前情報 {saved_bi:,}行 / "
                        f"気象 {saved_wx:,}行 / 空 {empty:,}レース "
                        f"({(time.time()-t0)/60:.1f}分)")

    logger.info(f"完了: {done:,}レース処理  直前情報 {saved_bi:,}行 / "
                f"気象 {saved_wx:,}行 / 空 {empty:,}レース")
    if empty:
        logger.info("※ 空のレースは中止などで元から公開されていない可能性がある")


if __name__ == "__main__":
    main()
