"""毎日の健全性チェック。動いているか、貯まっているかを1画面で出す。

検証は「毎日ちゃんと記録できていること」が前提になる。だが壊れ方は静かで、
2026-08-13 までに実際に起きたのは全部この種類だった:
    朝の更新が止まる / 判定が走らない / 確定オッズが入らない /
    賭けていない買い目が損益に混じる / 買い目が締切後に増える
どれもログを見に行かないと気づけなかった。毎日1回、結果だけを見る。

出力は docs/data/health.json にも書き、異常なときだけ PWA が知らせる。

使い方:
    python scripts/daily_check.py              # 今日を見る
    python scripts/daily_check.py 2026-08-12   # 日付指定
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text                     # noqa: E402
from src.ingestion.database import get_engine, init_db   # noqa: E402
from src.utils.helpers import load_config       # noqa: E402

# バッチからは cp932 のログにリダイレクトされる。絵文字を出すと
# UnicodeEncodeError で落ち、点検そのものが動かない（2026-08-14 に発生）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

JST = timezone(timedelta(hours=9))
DATA = Path(__file__).resolve().parent.parent / "docs" / "data"
CANDIDATE_REASON = "候補ルール(混合)"


def q1(conn, sql: str, **p):
    return conn.execute(text(sql), p).scalar() or 0


def main() -> None:
    cfg = load_config()
    init_db(cfg)
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(JST).date()
    now = datetime.now(JST)
    rule = cfg.get("operation", {}).get("candidate_rule") or {}
    live_since = str(cfg.get("operation", {}).get("live_since") or "1970-01-01")

    checks: list[tuple[str, bool, str]] = []

    with get_engine().connect() as conn:
        # 2026-08-22 以降、朝の買い目はクラウドが使い捨てDBで作る。
        # そのため「買い目が出たか」は履歴DBではなく docs/data の JSON で見る。
        # 履歴DBに今日のレースが入るのはローカル同期（13:00 かログオン時）以降で、
        # それまで 0 レースなのは正常。ここを DB で見ていると毎日誤警報になる。
        bets_json_path = DATA / f"bets_{d}.json"
        try:
            cloud_bets = len(json.loads(bets_json_path.read_text(encoding="utf-8")))
        except Exception:
            cloud_bets = 0
        picks_due = now.date() > d or now.hour >= 10
        checks.append(("買い目(クラウド)", (not picks_due) or cloud_bets > 0,
                       f"{cloud_bets}本" + ("（作成前）" if not cloud_bets and not picks_due else "")))

        races = q1(conn, "SELECT COUNT(*) FROM races WHERE race_date=:d", d=str(d))
        # 履歴DBへの取り込みはローカル同期の仕事。13:00 のトリガーを見込んで
        # 14 時以降だけ厳しく見る。
        synced_yet = now.date() > d or now.hour >= 14
        checks.append(("履歴DB取り込み", (not synced_yet) or races > 0,
                       f"{races}レース" + ("（同期前）" if not races and not synced_yet else "")))

        bets = q1(conn, """SELECT COUNT(*) FROM bets b JOIN races r ON r.id=b.race_id
                           WHERE r.race_date=:d AND b.is_pass=0""", d=str(d))
        checks.append(("買い目生成", races == 0 or bets > 0, f"{bets}本"))

        # 組合せごとの model_prob が残っているレースの割合。
        # 1本も買わないレースを1行に畳むと probs_<日付>.json からレースごと
        # 落ち、クラウドの refresh_odds がそのレースを一切見られない。
        # 静かに効くので気づけなかった: 08-17 は 36/168、08-18 は 9/144 しか
        # 対象になっておらず、候補ルールが5日で1本しか貯まらなかった。
        scored = q1(conn, """SELECT COUNT(DISTINCT r.id) FROM races r
                             WHERE r.race_date=:d AND EXISTS
                             (SELECT 1 FROM bets b WHERE b.race_id=r.id
                                AND b.model_prob IS NOT NULL)""", d=str(d))
        checks.append(("予測対象", races == 0 or scored / races >= 0.90,
                       f"{scored}/{races}レース"
                       + (f" ({scored / races * 100:.0f}%)" if races else "")))

        done = q1(conn, """SELECT COUNT(DISTINCT r.id) FROM races r
                           WHERE r.race_date=:d AND EXISTS
                           (SELECT 1 FROM race_results rr WHERE rr.race_id=r.id)""", d=str(d))
        # 開催中の日は途中で当然なので、21時以降だけ厳しく見る
        late = now.date() > d or now.hour >= 21
        checks.append(("結果収集", (not late) or (races and done / races >= 0.95),
                       f"{done}/{races}レース"))

        judged = q1(conn, """SELECT COUNT(*) FROM bets b JOIN races r ON r.id=b.race_id
                             WHERE r.race_date=:d AND b.is_pass=0 AND b.is_hit IS NOT NULL""",
                    d=str(d))
        checks.append(("判定", (not late) or bets == 0 or judged / bets >= 0.90,
                       f"{judged}/{bets}本"))

        # 2026-08-21 以降、当日の板は is_final=0、レース後の精算値は is_final=1。
        # 確定オッズはレース後の再取得でしか入らないので、開催中の日に
        # 「確定オッズが無い」のは正常。翌日以降だけ厳しく見る。
        def _full(is_final: int) -> int:
            return q1(conn, f"""SELECT COUNT(*) FROM (
                                  SELECT o.race_id FROM odds o JOIN races r ON r.id=o.race_id
                                   WHERE r.race_date=:d AND o.bet_type='nirenfuku'
                                     AND o.is_final={is_final} AND o.odds>0
                                   GROUP BY o.race_id HAVING COUNT(*)=15)""", d=str(d))
        board, full = _full(0), _full(1)
        # 朝の収集時点では2連複15通りのうち数通りしか値が出ていないのが普通で、
        # 揃うレースはもともと少ない。合否にするとほぼ毎日 NG になるので数だけ出す。
        # 締切間際の板（買える値）はクラウドが15分ごとに JSON へ書いており、
        # DB には入らない。DB の板はあくまで朝のスナップショット。
        checks.append(("当日の板(朝)", True,
                       f"{board}/{races}レース"
                       + (f" ({board / races * 100:.0f}%)" if races else "")))
        done_day = now.date() > d
        checks.append(("確定オッズ", (not done_day) or (races and full / races >= 0.90),
                       f"{full}/{races}レース"
                       + (f" ({full / races * 100:.0f}%)" if races else "")
                       + ("（レース後に取得）" if not done_day else "")))

        cand_today = q1(conn, """SELECT COUNT(*) FROM bets b JOIN races r ON r.id=b.race_id
                                 WHERE r.race_date=:d AND b.pass_reason=:cr""",
                        d=str(d), cr=CANDIDATE_REASON)
        cand_final = q1(conn, """SELECT COUNT(*) FROM bets b JOIN races r ON r.id=b.race_id
                                 WHERE r.race_date=:d AND b.pass_reason=:cr
                                   AND b.is_final_pick=1""", d=str(d), cr=CANDIDATE_REASON)
        checks.append(("候補ルール記録", True, f"{cand_today}本（確定{cand_final}本）"))

        # 累計の進み具合。締切前に確定したものだけが検証に使える。
        cum = q1(conn, """SELECT COUNT(*) FROM bets b JOIN races r ON r.id=b.race_id
                          WHERE b.pass_reason=:cr AND b.is_final_pick=1
                            AND r.race_date>=:s""", cr=CANDIDATE_REASON, s=live_since)
        cum_judged = q1(conn, """SELECT COUNT(*) FROM bets b JOIN races r ON r.id=b.race_id
                                 WHERE b.pass_reason=:cr AND b.is_final_pick=1
                                   AND b.is_hit IS NOT NULL AND r.race_date>=:s""",
                        cr=CANDIDATE_REASON, s=live_since)
        ret = q1(conn, """SELECT SUM(COALESCE(b.actual_payout,0)) FROM bets b
                          JOIN races r ON r.id=b.race_id
                          WHERE b.pass_reason=:cr AND b.is_final_pick=1
                            AND b.is_hit IS NOT NULL AND r.race_date>=:s""",
                 cr=CANDIDATE_REASON, s=live_since)
        hits = q1(conn, """SELECT COUNT(*) FROM bets b JOIN races r ON r.id=b.race_id
                           WHERE b.pass_reason=:cr AND b.is_final_pick=1
                             AND b.is_hit=1 AND r.race_date>=:s""",
                  cr=CANDIDATE_REASON, s=live_since)

    meta = {}
    try:
        meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    age = None
    if meta.get("last_refreshed"):
        age = (now - datetime.fromisoformat(meta["last_refreshed"])).total_seconds() / 60
    racing_hours = 10 <= now.hour <= 21 and now.date() == d
    checks.append(("クラウド更新",
                   age is not None and (not racing_hours or age <= 40),
                   "未取得" if age is None else f"{age:.0f}分前"))

    ng = [c for c in checks if not c[1]]
    print(f"=== {d} デイリーチェック（{now:%H:%M} 時点）===")
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else 'NG'}] {name:14} {detail}")

    target = int(rule.get("stage2_min_bets") or 182)
    stage1 = str(rule.get("stage1_date") or "")
    print(f"\n--- 候補ルールの検証（{rule.get('name', '?')}）---")
    print(f"  締切前に確定した買い目: 累計 {cum}本（判定済 {cum_judged}本）")
    if cum_judged:
        roi = ret / (cum_judged * 100) * 100
        se = (math.sqrt((hits / cum_judged) * (1 - hits / cum_judged))
              * (ret / max(hits, 1) / 100) / math.sqrt(cum_judged) * 100)
        print(f"  暫定成績: 的中{hits / cum_judged * 100:.1f}%  回収{roi:.0f}% ±{2 * se:.0f}")
    if stage1 and str(d) < stage1:
        print(f"  第1段階（オッズ下振れの測定）: {stage1}")
    print(f"  第2段階の目安: {target}本  → 残り {max(target - cum, 0)}本"
          f"（1日6.6本なら約{max(target - cum, 0) / 6.6:.0f}日）")

    out = {
        "date": str(d), "checked_at": now.isoformat(),
        "checks": [{"name": n, "ok": o, "detail": t} for n, o, t in checks],
        "ng": [n for n, _, _ in ng],
        "candidate": {"today": cand_today, "cumulative": cum,
                      "judged": cum_judged, "target": target},
    }
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "health.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    if ng:
        print(f"\n[!] 異常 {len(ng)}件: {', '.join(n for n, _, _ in ng)}")
        sys.exit(1)
    print("\nすべて正常")


if __name__ == "__main__":
    main()
