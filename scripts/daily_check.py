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
# 現在の候補ルールの見送り理由。棄却した market_blend("候補ルール(混合)")は
# 別物なので混ぜない — 成績を合算すると、棄却済みのルールが新しい候補の
# 数字を汚す。過去分は memory / git 履歴に残っている。
CANDIDATE_REASON = "候補ルール(価値1点)"


def q1(conn, sql: str, **p):
    return conn.execute(text(sql), p).scalar() or 0


def json_integrity_checks(d, data_dir: Path, prev_health: dict | None = None):
    """PWA が実際に読む JSON の中身を検査する。

    件数と鮮度だけを見ていたため、2026-08 の1週間に起きた4件を**全部
    素通りさせた**（08-29 は「すべて正常」と出していた）。壊れ方はどれも
    「行はあるが中身が空」「ファイル同士が噛み合わない」で、件数では出ない。

        08-26 出走表と予測が全消し   → 出走表と予測
        08-29 締切時刻が全消し       → 締切時刻
        08-26/27 採番の食い違い      → probsとracesの対応 / 買い目とレースの対応
        08-26/27 買い目が31→11本    → 買い目の目減り

    画面が引く経路をそのままなぞる（別の判定を書くと食い違って気づけない）。
    戻り値: [(名前, OKか, 詳細), ...] と、次回に渡す買い目の最大数。
    """
    prev_health = prev_health or {}
    checks: list[tuple[str, bool, str]] = []

    def _load(name):
        try:
            return json.loads((data_dir / f"{name}_{d}.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    races_j, bets_j, probs_j = _load("races"), _load("bets"), _load("probs")

    if races_j:
        no_ent = sum(1 for r in races_j if not r.get("entries"))
        no_prd = sum(1 for r in races_j if not r.get("predictions"))
        checks.append(("出走表と予測", no_ent == 0 and no_prd == 0,
                       f"欠け 出走表{no_ent} 予測{no_prd} / {len(races_j)}レース"))
        no_ct = sum(1 for r in races_j if not r.get("closing_time"))
        checks.append(("締切時刻", no_ct == 0,
                       f"欠け {no_ct}/{len(races_j)}レース"
                       + ("　※買い目が確定しません" if no_ct else "")))

    if races_j and bets_j:
        rid = {r.get("id") for r in races_j}
        by_key = {(r.get("stadium"), r.get("race_no")) for r in races_j}
        lost = [b for b in bets_j
                if b.get("race_id") not in rid
                and (b.get("stadium_name"), b.get("race_no")) not in by_key]
        checks.append(("買い目とレースの対応", not lost,
                       f"引けない {len(lost)}/{len(bets_j)}本"))

    if races_j and probs_j:
        # 実際に refresh_odds が使う関数で引く。別の判定にすると食い違う。
        # 採番が違っても index_probs_by_race が場とレース番号で橋渡しするので、
        # 「食い違っているか」ではなく「**買い目を作れるか**」を見る。
        # 橋渡しが要った事実は詳細に出す（ローカルが races を書いた印）。
        try:
            from main import index_probs_by_race
            rid = {r["id"] for r in races_j}
            need = len(probs_j.get("races", []))
            remapped = sum(1 for e in probs_j.get("races", []) if e.get("race_id") not in rid)
            got = index_probs_by_race(probs_j, races_j, [r["id"] for r in races_j])
            checks.append(("probsとracesの対応", need > 0 and len(got) >= need,
                           f"{len(got)}/{need}レース"
                           + (f"（採番違いを{remapped}件橋渡し）" if remapped else "")
                           + ("　※買い目が作れません" if len(got) < need else "")))
        except Exception as e:
            checks.append(("probsとracesの対応", False, f"確認できず: {e}"))

    # 日中に買い目が激減していないか。件数>0 では見えない壊れ方だった
    peak = 0
    if str(prev_health.get("date")) == str(d):
        peak = int(prev_health.get("bets_peak") or 0)
    now_n = len(bets_j) if bets_j else 0
    peak = max(peak, now_n)
    if peak >= 10:
        checks.append(("買い目の目減り", now_n >= peak * 0.8,
                       f"いま{now_n}本 / 本日最大{peak}本"))
    return checks, peak


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
        # 買い目を作るのはクラウドで、ローカルDBに入るのは 22:30 の判定が
        # _sync_bets_from_json を呼ぶとき。それまで 0 なのが正常なので、
        # 日中に厳しく見ると毎日 NG が鳴る（「予測対象」と同じ誤報。
        # 上の「買い目(クラウド)」が当日ぶんの本当の見張りになっている）。
        judged_yet = now.date() > d or now.hour >= 23
        checks.append(("買い目生成", (not judged_yet) or races == 0 or bets > 0,
                       f"{bets}本" + ("（判定前）" if not bets and not judged_yet else "")))

        # 組合せごとの model_prob が残っているレースの割合。
        # 1本も買わないレースを1行に畳むと probs_<日付>.json からレースごと
        # 落ち、クラウドの refresh_odds がそのレースを一切見られない。
        # 静かに効くので気づけなかった: 08-17 は 36/168、08-18 は 9/144 しか
        # 対象になっておらず、候補ルールが5日で1本しか貯まらなかった。
        #
        # 数えるのは probs_<日付>.json であって DB ではない。予測はクラウドで
        # 走り、ローカルDBには「買った買い目」しか同期されないので、DB を見ると
        # 常に 20% 前後になって毎日 NG が鳴る（2026-08-23 の誤報はこれ）。
        # refresh_odds が実際に読むファイルを直接見るのが正しい。
        def _json_len(name: str, key: str | None = None) -> int:
            try:
                obj = json.loads((DATA / name).read_text(encoding="utf-8"))
            except Exception:
                return 0
            return len(obj.get(key, [])) if key else len(obj)

        scored = _json_len(f"probs_{d}.json", "races")
        listed = _json_len(f"races_{d}.json") or races
        checks.append(("予測対象", listed == 0 or scored / listed >= 0.90,
                       f"{scored}/{listed}レース"
                       + (f" ({scored / listed * 100:.0f}%)" if listed else "")))

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

    # PWA が実際に読む JSON の中身（関数の説明に経緯）
    prev_health = {}
    try:
        prev_health = json.loads((DATA / "health.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    json_checks, peak = json_integrity_checks(d, DATA, prev_health)
    checks.extend(json_checks)

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

    # 通知が飛ばせる状態か。未設定だと notify.py は標準出力に書くだけで
    # 正常終了するため、異常が起きても誰にも届かないまま気づけない
    # （2026-08-24 まで毎晩 "[dry-run: ... not set]" がログに出続けていた）。
    # 監視が届かないことこそ検知したい。判定は notify.py と同じ関数を使う
    # ——ここだけ別の探し方をすると、また食い違って気づけなくなる。
    try:
        from notify import webhook_url
        webhook = webhook_url()
    except Exception as e:
        webhook = None
        print(f"  (通知設定の確認に失敗: {e})")
    checks.append(("通知の宛先", bool(webhook),
                   "設定済み" if webhook else "未設定（異常が起きても届きません）"))

    # 候補ルールの一番の急所: 板から見込んだオッズが、実際の確定オッズより
    # 高く出ていないか。高い＝期待値を過大評価している＝過去2件の候補を
    # 潰したのと同じ「オッズの上振れを拾う」構造。
    # ⚠️ 危険は 1.0 を**上回る**方向。1.0未満は推定が保守的なだけで安全側。
    # 見込みオッズは DB に列が無いので expected_value / model_prob で戻す。
    ratio_max = float(rule.get("monitor_ratio_max") or 1.15)
    with get_engine().connect() as conn:
        rs = [r[0] for r in conn.execute(text(f"""
            SELECT (b.expected_value / b.model_prob) / o.odds
            FROM bets b JOIN races r ON r.id = b.race_id
            JOIN odds o ON o.race_id = b.race_id AND o.bet_type = b.bet_type
                       AND o.combination = b.combination AND o.is_final = 1
            WHERE b.pass_reason = :cr AND b.model_prob > 0 AND o.odds > 0
              AND r.race_date >= :s"""), {"cr": CANDIDATE_REASON, "s": live_since})]
    if len(rs) >= 20:
        rs.sort()
        med = rs[len(rs) // 2]
        checks.append(("見込みオッズの精度", med <= ratio_max,
                       f"推定/確定 中央値 {med:.2f}（{len(rs)}本・上限{ratio_max:.2f}）"
                       + ("　※過大評価" if med > ratio_max else "")))
    else:
        checks.append(("見込みオッズの精度", True,
                       f"{len(rs)}本（20本以上で判定）"))

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
        # その日の買い目の最大数。次回の点検が「目減りしていないか」に使う
        "bets_peak": peak,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "health.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    if ng:
        print(f"\n[!] 異常 {len(ng)}件: {', '.join(n for n, _, _ in ng)}")
        sys.exit(1)
    print("\nすべて正常")


if __name__ == "__main__":
    main()
