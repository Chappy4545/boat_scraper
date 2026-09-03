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
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
# 現在の候補ルールの見送り理由。棄却した market_blend("候補ルール(混合)")は
# 別物なので混ぜない — 成績を合算すると、棄却済みのルールが新しい候補の
# 数字を汚す。過去分は memory / git 履歴に残っている。
CANDIDATE_REASON = "候補ルール(価値1点)"


def q1(conn, sql: str, **p):
    return conn.execute(text(sql), p).scalar() or 0


# 賭式を2連複だけから6つへ広げた日。これより前の日は1賭式しか無いのが正常。
SIX_BET_TYPES_SINCE = "2026-08-31"


def _configured_bet_types() -> set[str]:
    """config が「作るはず」としている賭式（買う + 記録のみ）。

    ここに一覧を書き写すと設定を変えたときに黙って食い違うので、必ず
    config から読む。読めなければ空集合を返し、検査自体を出さない
    （確認できないことを「異常なし」と報告しないため）。
    """
    from src.models.plackett_luce import BET_TYPE_JP
    try:
        bet = load_config()["betting"]
        jp = list(bet.get("bet_types") or []) + list(bet.get("paper_bet_types") or [])
        return {BET_TYPE_JP[x] for x in jp if x in BET_TYPE_JP}
    except Exception:
        return set()


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

    # 賭式が欠けていないか。片方の経路だけ古いコードのままだと黙って賭式が
    # 減る（画面には「買い目はある」と出るので件数では気づけない）。
    # ⚠️ 件数ではなく**種類**で見る。2連複だけ135本あっても異常。
    # 期待する賭式は config から読む（ここに書き写すと設定変更で食い違う）。
    # 6賭式より前の日に当てると全部 NG になるので、切替日以降だけ見る。
    want = _configured_bet_types()
    if bets_j and want and str(d) >= SIX_BET_TYPES_SINCE:
        got = {b.get("bet_type") for b in bets_j}
        miss = want - got
        checks.append(("賭式の欠け", not miss,
                       f"{len(got & want)}/{len(want)}賭式"
                       + (f"　※欠け {' '.join(sorted(miss))}" if miss else "")))

    # 一度確定した買い目が消えていないか。
    # 日中に消えてよいのは「まだ確定していない買い目」だけ（オッズが動けば
    # EV が変わるので入れ替わる）。**確定したものが消えるのは記録の破壊**で、
    # 後から損益が書き換わる。クラウドが15分ごとにコミットしているので、
    # その履歴を遡ればその日の全ての状態を見られる。
    # ⚠️ 見るのは「いま欠けているか」（unrestored）であって「一度でも消えたか」
    # ではない。後者は直しても消えないので、テストも通知も永久に赤くなり、
    # 赤を無視する癖がつく。飛んだ回数は参考として併記する。
    lost_final = _final_picks_lost(d, data_dir)
    if lost_final is not None:
        n_lost, n_rev = lost_final
        checks.append(("確定買い目の保全", n_lost == 0,
                       f"いま欠けている確定 {n_lost}本 / {n_rev}版"
                       + ("　※損益が書き換わります" if n_lost else "")))

    # 締切より後に初めて現れた「金額つき」買い目。買えなかったのに
    # 損益に入る。2026-08-31 に朝のクラウド実行で発覚（詳細は
    # unbuyable_money_bets の説明）。生成側は直したので、ここは再発の見張り。
    revs = bets_revisions(d, data_dir)
    if revs:
        bad = unbuyable_money_bets(revs, d)
        checks.append(("締切後に生えた買い目", not bad,
                       f"{len(bad)}本"
                       + (f"　※{bad[0][0]}R{bad[0][1]} は締切{bad[0][2]}の"
                          f"{bad[0][4]}分後に出た" if bad else "")))
    return checks, peak


def final_pick_key(b: dict) -> tuple:
    """確定買い目を版をまたいで同一視するためのキー。

    ⚠️ race_id を使ってはいけない。クラウドとローカルで採番の体系が違い
    （2026-08-26 実測 クラウド 73〜 / 履歴DB 36936〜 で重なりゼロ）、書き手が
    替わった日は同じ買い目が別物に見える。2026-08-31 に最初この関数を
    race_id で書いたところ、08-28 に「確定が32本消えた」と誤検知した
    （実際は0本）。場+レース番号で見る（src/export.py の _race_key と同じ）。
    """
    return (b.get("stadium_name"), b.get("race_no"),
            b.get("bet_type"), b.get("combination"))


def lost_final_picks(revisions) -> set[tuple]:
    """版の並び（古い順）を受け取り、一度確定した後に消えた買い目を返す。

    git から切り離してあるのはテストのため。git 越しだと「消えた版」を
    作れず、検査が本当に落ちるかを確かめられない。
    """
    seen: set[tuple] = set()
    lost: set[tuple] = set()
    for bets in revisions:
        keys = {final_pick_key(b) for b in bets if b.get("is_final_pick")}
        lost |= (seen - keys)
        seen |= keys
    return lost


def unrestored_final_picks(revisions) -> set[tuple]:
    """一度確定したのに、**いまも**記録から欠けている買い目を返す。

    `lost_final_picks` との違いは時制。あちらは「その日に起きたか」を溜める
    ので、直しても消えない（毎晩の通知はそれでよい。事故は事故）。
    こちらは「いま壊れているか」なので、復元すれば消える。

    ⚠️ 使い分けを間違えないこと。両方を「異常」として扱うと、
    直しようのない過去の事実でテストが永久に赤くなり、赤を無視する癖がつく。
    2026-09-02 の12本がまさにこれだった（クラウドの版から復元済み）。
    """
    if not revisions:
        return set()
    latest = {final_pick_key(b) for b in revisions[-1] if b.get("is_final_pick")}
    return lost_final_picks(revisions) - latest


def unbuyable_money_bets(revisions, d) -> list[tuple]:
    """締切より後に初めて現れた「金額つき」買い目を返す。

    ⚠️ これは買えなかった買い目。損益に入ると数字が嘘になる。
    2026-08-31 実測: 芦屋R2 は締切の46分後、R3 は20分後に初めて JSON に
    現れ、どちらも 500円 が付いていた。朝のクラウド実行が最速レースの
    締切に間に合っていなかった（初回の書き出しが 09:43、締切は 08:58/09:24）。

    ⚠️ `is_final_pick` で代用してはいけない。確定しなかった買い目には
    「更新が止まって確定できなかっただけで、朝から見えていた」ものが
    混ざる（08-26 の8本がこれ）。**初めて現れた時刻**で見ること。

    revisions は (コミット時刻, 買い目リスト) を古い順に並べたもの。
    """
    from datetime import datetime as _dt

    first_seen: dict[tuple, object] = {}
    out = []
    for when, bets in revisions:
        for b in bets:
            if (b.get("recommended_amount") or 0) <= 0:
                continue
            k = final_pick_key(b)
            if k in first_seen:
                continue
            first_seen[k] = when
            ct = b.get("closing_time")
            if not ct:
                continue
            try:
                close = _dt.strptime(f"{d} {ct}", "%Y-%m-%d %H:%M").replace(
                    tzinfo=when.tzinfo)
            except (ValueError, TypeError):
                continue
            if when > close:
                out.append((b.get("stadium_name"), b.get("race_no"), ct,
                            when.strftime("%H:%M"),
                            int((when - close).total_seconds() // 60)))
    return out


def night_runs(lines) -> list[tuple[str, bool]]:
    """夜間処理のログ行から (開始時刻, 完走したか) を古い順に返す。

    ログの形は daily_judge.bat が書く3種類:
        [日付 時刻] JUDGE start / JUDGE done / JUDGE failed
    start の次に done|failed が来る前に別の start が来たら、その回は
    **完走していない**（＝途中で殺された）。
    """
    import re

    runs: list[tuple[str, bool]] = []
    pat = re.compile(r"\[([^\]]+)\]\s+JUDGE (start|done|failed)")
    for ln in lines:
        m = pat.search(ln)
        if not m:
            continue
        when, kind = m.group(1).strip(), m.group(2)
        if kind == "start":
            runs.append((when, False))
        elif runs:
            runs[-1] = (runs[-1][0], True)
    return runs


def _night_run_check() -> tuple[str, bool, str]:
    """直近の夜間処理が完走しているか。

    今まさに走っている回（daily_check は judge の後に動く）はまだ done を
    書いていないので、最後の1回は判定から外す。
    """
    log = ROOT / "logs" / "task_judge.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ("夜間処理の完走", True, "ログなし（判定せず）")
    runs = night_runs(lines)
    if len(runs) < 2:
        return ("夜間処理の完走", True, f"{len(runs)}回のみ（判定せず）")
    past = runs[:-1][-7:]           # 直近7回（今走っている回は除く）
    dead = [w for w, ok in past if not ok]
    return ("夜間処理の完走", not dead,
            f"直近{len(past)}回中 未完走{len(dead)}回"
            + (f"　※{dead[-1]} が途中で止まっています" if dead else ""))


def _final_picks_lost(d, data_dir: Path) -> tuple[int, int] | None:
    """その日の bets JSON の全リビジョンを git から読んで判定する。

    git が無い / 履歴が浅い（CI の shallow clone）ときは None。
    確認できないことを「異常なし」と報告しないため、黙って項目を出さない。
    """
    import subprocess

    rel = f"docs/data/bets_{d}.json"
    root = data_dir.parent.parent          # docs/data → リポジトリ直下

    def _git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)

    try:
        r = _git("log", "--format=%H", "--", rel)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        revs = r.stdout.split()
        out = []
        for sha in reversed(revs):         # 古い順
            b = _git("show", f"{sha}:{rel}")
            if b.returncode == 0:
                out.append(json.loads(b.stdout))
        # 作業ツリーの現物を最後に足す。直した直後はまだコミットされて
        # いないので、これが無いと「復元済み」を復元前と見なしてしまう。
        cur = data_dir / f"bets_{d}.json"
        if cur.exists():
            try:
                out.append(json.loads(cur.read_text(encoding="utf-8")))
            except Exception:
                pass
        return len(unrestored_final_picks(out)), len(revs)
    except Exception:
        return None


def bets_revisions(d, data_dir: Path):
    """その日の bets JSON の全版を (コミット時刻, 中身) で古い順に返す。

    git が無い / 履歴が浅いときは None（確認できないことを「異常なし」と
    報告しないため、検査ごと出さない）。
    """
    import subprocess
    from datetime import datetime as _dt

    rel = f"docs/data/bets_{d}.json"
    root = data_dir.parent.parent

    def _git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)

    try:
        r = _git("log", "--format=%H %cI", "--", rel)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        out = []
        for line in reversed(r.stdout.strip().splitlines()):   # 古い順
            sha, when = line.split(None, 1)
            b = _git("show", f"{sha}:{rel}")
            if b.returncode == 0:
                out.append((_dt.fromisoformat(when.strip()).astimezone(JST),
                            json.loads(b.stdout)))
        return out
    except Exception:
        return None


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

    # 夜間処理が完走したか。**途中で死んでも誰も知らない**のが一番の穴だった。
    # 2026-08-28 22:30 の実行は DB 初期化まで書いて消滅し（PC が落ちた）、
    # 8/29 は起動すらせず、8/30 09:00 の取りこぼし起動でようやく 8/29 分が
    # 処理された。その間ログには "JUDGE start" が done 無しで残っていただけで、
    # 警告は一度も出ていない。
    checks.append(_night_run_check())

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
    # ⚠️ 2026-08-31 に判定の仕方を変えた。
    # 以前は「中央値 <= 1.15」という**水準**で見ており、毎晩 NG が出ていた
    # （実測 1.36）。だがこの縮みは**構造的**で、EV で選ぶ限り必ず起きる:
    #
    #     EV 1.2-1.5  1.292 / EV 1.5-2.0  1.588
    #     EV 2.0-3.0  1.986 / EV 3.0以上  3.333
    #     記録のみ    1.000  ← EV で選んでいないので縮まない
    #
    # 縮みは市場の性質ではなく「EVが高い＝オッズが上振れしている組合せを
    # 選んでいる」ことの副作用（optimizer's curse）。水準で鳴らし続けても
    # 直しようがなく、**本物の異常が埋もれる**。
    # 見るべきは「いつもと違うか」。直近7日と、それ以前を比べる。
    # `scripts/odds_shrink.py` が層ごとの内訳を出す。
    def _ratios(since, until=None):
        cond = "AND r.race_date < :u" if until else ""
        p = {"s": since, "cr": CANDIDATE_REASON}
        if until:
            p["u"] = until
        with get_engine().connect() as conn:
            return sorted(r[0] for r in conn.execute(text(f"""
                SELECT (b.expected_value / b.model_prob) / o.odds
                FROM bets b JOIN races r ON r.id = b.race_id
                JOIN odds o ON o.race_id = b.race_id AND o.bet_type = b.bet_type
                           AND o.combination = b.combination AND o.is_final = 1
                WHERE b.pass_reason = :cr AND b.model_prob > 0 AND o.odds > 0
                  AND b.expected_value IS NOT NULL
                  AND r.race_date >= :s {cond}"""), p))

    def _med(xs):
        return xs[len(xs) // 2] if xs else float("nan")

    recent_from = str(d - timedelta(days=7))
    recent, past = _ratios(recent_from), _ratios(live_since, recent_from)
    if len(recent) >= 20 and len(past) >= 20:
        mr, mp_ = _med(recent), _med(past)
        # 悪化（縮みが強まる）方向だけを異常とする。改善は歓迎。
        worse = mr > mp_ * 1.25
        checks.append(("見込みオッズの縮み", not worse,
                       f"直近7日 {mr:.2f} / それ以前 {mp_:.2f}"
                       f"（{len(recent)}本 vs {len(past)}本）"
                       + ("　※急に強まっています" if worse else "")))
    elif len(recent) + len(past) >= 20:
        allr = sorted(recent + past)
        checks.append(("見込みオッズの縮み", True,
                       f"中央値 {_med(allr):.2f}（{len(allr)}本・比較にはまだ日数不足）"))
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
        notify_ng(d, ng)
        sys.exit(1)
    print("\nすべて正常")


def notify_ng(d, ng) -> bool:
    """異常があったときだけ手元へ飛ばす。

    ⚠️ これまで daily_check は「通知先が設定されているか」を確かめるだけで、
    **自分では一度も送っていなかった**。異常は health.json と画面バナーに
    しか出ず、アプリを開くまで気づけない。2026-08-28 の夜間処理の未完走が
    3日間気づかれなかったのはこれ（[[project_update_reliability]]）。

    正常な日は送らない。毎晩届くと読まなくなる。
    戻り値は「送ろうとしたか」。宛先未設定なら False。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from notify import _send, webhook_url
        if not webhook_url():
            print("  (通知先が未設定のため送りません)")
            return False
        lines = [f"⚠️ {d} の点検で {len(ng)}件の異常"]
        for n, _ok, detail in ng:
            lines.append(f"・{n}: {detail}")
        _send("\n".join(lines))
        return True
    except SystemExit:
        # notify._send は失敗時に exit(1) する。点検の結果表示まで
        # 巻き込まれないよう、ここで止める。
        print("  (通知の送信に失敗しました)")
        return False
    except Exception as e:
        print(f"  (通知を送れませんでした: {e})")
        return False


if __name__ == "__main__":
    main()
