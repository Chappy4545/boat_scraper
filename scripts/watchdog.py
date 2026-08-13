"""運用中の成績を監視し、統計的に異常なときだけ知らせる。

日々の数字は誤差が大きく、1日の結果で判断すると必ず間違える
（実測: 20本でROI 49% は、真のROIが120%でも3回に1回起きる）。
そこで「真のROIが120%なら滅多に起きない水準」を割ったときだけ警告する。

判定線（1本あたりの標準偏差 約220円＝的中率31%・平均配当4.76倍 から算出）:
    100本 → 76%未満
    200本 → 89%未満
    300本 → 95%未満
    500本 → 100%未満
50本未満は誤差が±31%もあり判断できないので何も言わない。

使い方:
    python scripts/watchdog.py           # 判定して必要なら通知
    python scripts/watchdog.py --report  # 通知せず現状だけ表示
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def live_since() -> str:
    """運用開始日。これより前は旧ルール・旧モデルの買い目なので混ぜない。

    PWA の収支表示（src/export.py の live_since）と同じ値を使う。
    ここだけ別の日付を持っていると、通知と画面で数字が食い違う。
    """
    from src.export import live_since as _ls
    return _ls()

# 1本あたりのばらつき（円／100円賭け）。実測の的中率と平均配当から。
SD_PER_BET = 476 * math.sqrt(0.309 * (1 - 0.309))
ASSUMED_ROI = 120.0     # 「勝てている」と仮定した場合のROI
MIN_BETS = 50           # これ未満は判断しない


def threshold(n: int) -> float:
    """n本時点で「異常」と言える下限（真のROIが120%なら2SE下）。"""
    return ASSUMED_ROI - 2 * (SD_PER_BET / math.sqrt(n))


def fetch():
    from sqlalchemy import text
    from src.ingestion.database import get_engine, init_db
    from src.utils.helpers import load_config

    init_db(load_config())
    sql = """
        SELECT COUNT(*) AS bets,
               SUM(CASE WHEN b.is_hit = 1 THEN 1 ELSE 0 END) AS hits,
               SUM(b.recommended_amount) AS invested,
               SUM(CASE WHEN b.is_hit = 1
                        THEN CAST(b.recommended_amount * b.actual_payout / 100 AS INTEGER)
                        ELSE 0 END) AS returned
        FROM bets b JOIN races r ON r.id = b.race_id
        WHERE b.is_pass = 0 AND b.is_hit IS NOT NULL AND r.race_date >= :since
    """
    with get_engine().connect() as conn:
        row = conn.execute(text(sql), {"since": live_since()}).fetchone()
    return {
        "bets": int(row[0] or 0), "hits": int(row[1] or 0),
        "invested": int(row[2] or 0), "returned": int(row[3] or 0),
    }


def judge(s: dict) -> tuple[str, str]:
    """(状態, メッセージ) を返す。状態は ok / watch / alert / early。"""
    n, inv = s["bets"], s["invested"]
    if n < MIN_BETS:
        return "early", (f"判定済み {n} 本（{MIN_BETS} 本までは誤差が大きく判断できません）")

    roi = s["returned"] / inv * 100 if inv else 0.0
    lim = threshold(n)
    hit = s["hits"] / n * 100
    se = SD_PER_BET / math.sqrt(n)
    head = (f"{n}本 的中{hit:.1f}% 回収率{roi:.1f}%（誤差±{se:.0f}%）"
            f" 損益{s['returned'] - inv:+,}円")

    if roi < lim:
        return "alert", (
            f"⚠️ 想定を下回っています\n{head}\n"
            f"この本数での警戒線は {lim:.0f}%。真のROIが120%なら滅多に割らない水準です。\n"
            f"原因を調べるか、ルールの見直しを検討してください。"
        )
    if roi < lim + se:
        return "watch", f"やや低調\n{head}\n警戒線 {lim:.0f}% に接近しています。"
    return "ok", f"想定内\n{head}\n警戒線 {lim:.0f}%"


def main():
    report_only = "--report" in sys.argv
    s = fetch()
    state, msg = judge(s)
    print(f"[{state}] {msg}")

    if report_only or state in ("ok", "early"):
        return
    # watch / alert のときだけ通知する（毎日鳴ると見なくなるため）
    try:
        from scripts.notify import _send
    except Exception:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from notify import _send
    _send(f"【成績監視】{date.today()}\n{msg}")


if __name__ == "__main__":
    main()
