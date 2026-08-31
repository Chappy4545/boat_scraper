"""DBから静的JSONファイルを生成し docs/data/ に出力する。
毎日の predict 後に実行し、GitHub Pages用データを更新する。
"""
import json
from datetime import date
from pathlib import Path

from sqlalchemy import func as sa_func, or_ as sa_or

# 検証中の候補ルールの見送り理由（main.CANDIDATE_REASONS と同じ値）。
# ここから main を import すると循環するので、定数を持つ。
#
# ⚠️ 必ず複数形で判定すること。棄却した market_blend の43本が DB に残って
# おり、単一の文字列で比べるとそれが本番ルールの成績に混ざる。
CANDIDATE_REASONS = ("候補ルール(混合)", "候補ルール(縮み補正)", "候補ルール(価値1点)",
                     "記録のみ(賭式検証)")
# 見送り理由 → 画面と JSON で使うルール名
CANDIDATE_RULE_OF = {"候補ルール(混合)": "market_blend",
                     "候補ルール(縮み補正)": "shrink_adj",
                     "候補ルール(価値1点)": "top1_value",
                     "記録のみ(賭式検証)": "record"}

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


def json_race_id(race_date, stadium_code, race_no) -> int:
    """JSON に出すレース番号。**DBの採番を外に出さないための計算値。**

        YYYYMMDD * 10000 + 場コード * 100 + レース番号
        2026-08-29 桐生(01) 1R → 202608290101

    なぜ必要か
    ----------
    JSON は2人が書く。クラウド(predict_cloud)はその日ぶんの使い捨てSQLite、
    ローカルは5月からの履歴DB。どちらも `Race.id`（自動採番）をそのまま
    出していたため、同じレースに2つの番号がついた。

        2026-08-26 実測  クラウド 1〜168 / 履歴DB 36864〜37031  重なりゼロ

    JSON にはどちらの体系かが書いていないので、2つのファイルを同時に読む処理
    （races×bets、probs×races、画面の買い目→レース）が**黙って空振り**する。
    実害は4件あり、いずれもエラーを出さずに数日〜2週間続いた:

        08-23 別の日のレースに買い目が116件挿入された
        08-26 買い目をタップしても選手も確率も出ない
        08-26/27 昼から買い目が生成されなくなる（31本→11本 / 23本→10本）
        08-24〜28 端末に古いJSが残り、買っていない候補が並ぶ

    消費側を1つずつ直しても、突き合わせは他にもある（実際 08-29 の点検で
    未修正が3箇所見つかった）。**書き手の側で番号を揃えるのが本筋。**

    この値は (日付, 場, レース番号) から決まるので、誰が書いても同じになる。
    最大 202612312412 で JS の安全整数に収まる。

    ⚠️ 過去のJSONはDB採番のまま。日ごとに3ファイルが揃っていればよいので
    移行は不要（古い日は古い体系で内部一貫している）。
    """
    try:
        ymd = int(str(race_date).replace("-", ""))
        return ymd * 10000 + int(stadium_code) * 100 + int(race_no)
    except (TypeError, ValueError):
        return None


# races JSON でレースごとに引き継ぐ項目。DB側が空のときだけ既存を使う。
#
# ⚠️ 2026-08-26 の修正では entries / predictions しか守っておらず、
# 08-29 に **closing_time と grade が同じ経路で消えた**（156レース全部）。
# ローカルが結果だけ collect して出走表を collect しなかった日は、DBに
# 締切時刻が入らない。export はそれを忠実に null で書き、クラウドが朝に
# 入れた値を潰す。締切時刻は refresh_odds が「確定させるか」を判断する要で、
# 消えると買い目がいつまでも確定しない。
#
# 項目を列挙して守る方式は、増えるたびに同じ事故が起きる。
# **DB側が空なら既存を使う**を既定にして、id や場のように必ずDBから来るものだけ
# 除く。is_night は bool(None)=False で「無い」と区別できないので対象外。
_RACE_KEEP_EXCLUDE = ("id", "race_date", "stadium", "race_no", "is_night")


def _race_key(r: dict):
    """JSON をまたいでレースを突き合わせるキー。

    ⚠️ `id` は使えない。クラウド(predict_cloud)は「その日ぶんの使い捨て
    SQLite」で export_day を回すので採番が別体系になる。2026-08-26 実測で
    クラウド 73〜 / 履歴DB 36936〜 と **重なりゼロ** だった。
    id で突き合わせた版は例外も出さず黙って0件引き継ぎ、ログには
    「168件」と出ていた。場とレース番号なら両者で一致する。
    """
    return (r.get("stadium"), r.get("race_no"))


def _keep_existing_race_details(races_path: Path, races_json: list[dict]) -> list[dict]:
    """既存 races JSON の中身を、DB側が空のレースにだけ引き継ぐ。

    呼び出し側(export_day)に経緯を書いた。要点は「クラウドが書いた中身を
    ローカルの判定が空で上書きする」経路を塞ぐこと。
    項目を列挙せず、_RACE_KEEP_EXCLUDE 以外はすべて対象にする。
    """
    if not races_path.exists():
        return races_json
    try:
        existing = json.loads(races_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"export: {races_path.name} を読めないので引き継ぎません: {e}")
        return races_json
    if not isinstance(existing, list):
        return races_json

    prev = {_race_key(r): r for r in existing if isinstance(r, dict)}
    restored: dict[str, int] = {}
    for r in races_json:
        old = prev.get(_race_key(r))
        if not old:
            continue
        for key, val in old.items():
            if key in _RACE_KEEP_EXCLUDE:
                continue
            if not r.get(key) and val:
                r[key] = val
                restored[key] = restored.get(key, 0) + 1
    if restored:
        detail = " ".join(f"{k}={n}" for k, n in sorted(restored.items()))
        logger.info(f"export: {races_path.name} の空欄を既存から引き継ぎました（{detail}）")
    return races_json


def export_day(target_date: date) -> dict:
    """指定日の races / bets JSON を **DBの中身で作り直す**。

    ⚠️ **繋いでいるDBにその日のデータが揃っているときだけ呼ぶこと。**
    正しい呼び出し元はクラウドの predict_cloud（当日ぶんの使い捨てSQLite）だけ。

    ローカルの履歴DBは予測も買い目も持たず、出走表や締切時刻が無い日もある。
    そこでこれを呼ぶと、欠けている分が null で上書きされて記録が壊れる
    （2026-08 の1週間で4回発生）。ローカルからは
    `fill_results_into_json()` を使うこと。そちらは書き足すだけで壊せない。

    引き継ぎ（_keep_existing_race_details）は最後の保険であって、
    これがあるから安全という意味ではない。守れるのは「既存 JSON にある項目」
    だけで、クラウドが一度も書いていない日は守れない。
    """
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
        # id は DB の採番ではなく計算値を出す（json_race_id の説明を参照）。
        # これで誰が書いても同じ番号になり、ファイル間の突き合わせが成立する。
        races_json = []
        for r, s in races:
            races_json.append({
                "result_order": order_map.get(r.id),
                "id": json_race_id(d, s.code, r.race_no) or r.id,
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
                          Bet.pass_reason.in_(CANDIDATE_REASONS)),
                    sa_func.date(Bet.created_at) <= Race.race_date)
            .order_by(Race.race_no, Bet.expected_value.desc())
            .all()
        )

        # 着順は上で race_ids 全体から order_map を作ってあるのでそれを使う
        # （judge_live も同じキー result_order で書き込む）
        bets_json = [
            {
                "bet_id": b.id,
                # races JSON の id と同じ計算値にする（json_race_id 参照）
                "race_id": json_race_id(d, s.code, r.race_no) or b.race_id,
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
                # ルール名は必ず入れる。クラウド(refresh_odds)が書く JSON と
                # 同じ形にしておくため。
                # ⚠️ 以前は候補ルールの行にだけ付けており、買う買い目は
                # rule が無い（＝null）だった。同じ買い目が経路によって
                # "r5" と null になり、rule で束ねる集計が割れる。
                # 2026-08-31 の実データにも「rule=null・500円」が2本あった
                # （締切前に確定できず DB 側の版が残ったもの）。
                "rule": CANDIDATE_RULE_OF.get(b.pass_reason,
                                              "record" if b.is_pass else "r5"),
            }
            for b, r, s in bets_raw
        ]

    date_str = str(d)
    races_path = DATA_DIR / f"races_{date_str}.json"
    bets_path = DATA_DIR / f"bets_{date_str}.json"

    # 出走表・予測を空で潰さない（bets と同じ事故が races でも起きていた）。
    #
    # クラウド(predict_cloud)は「その日ぶんの使い捨てSQLite」で export_day を
    # 回すので entries も predictions も揃う。一方ローカルは履歴DBで同じ関数を
    # 回すが、そこには
    #   - predictions が無い（2026-08-23 以降。予測はクラウドの仕事にした）
    #   - entries も判定を先に回した日は、その時点でまだ入っていない
    # ため、クラウドが朝に書いた中身を空で上書きしてしまう。
    #
    # 2026-08-26 実測: クラウドが 00:44 に書いた 168レース(entries/predictions
    # とも168) を、ローカルの 13:02 の判定が 0/0 に潰していた。画面で買い目を
    # タップしても選手も確率も出ない状態になる。8/23・8/24 も同じ経路で
    # predictions だけが消えていた。
    #
    # 着順(result_order)は判定で更新する必要があるので、書き換えを止めるのでは
    # なくレース単位で引き継ぐ。DB側に中身があるときは常にそちらを優先する。
    races_json = _keep_existing_race_details(races_path, races_json)

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


def fill_results_into_json(target_date: date) -> dict:
    """既存の races / bets JSON に**結果だけを書き足す**（ローカルの判定用）。

    なぜ export_day を使わないか
    ---------------------------
    export_day は「DBの中身で JSON を作り直す」。クラウド(predict_cloud)の
    使い捨てSQLite はその日のデータが揃っているので正しく書けるが、
    **ローカルの履歴DBは中身が欠けている**:

        - 予測が無い（2026-08-23 に予測をクラウドの仕事にした）
        - 出走表が無い日がある（結果だけ collect した日）
        - 締切時刻・グレードが無い日がある（同上）
        - 買い目そのものが無い（クラウドが作るので）

    そこへ export_day を走らせると、欠けている分がそのまま null で上書きされる。
    2026-08 の1週間で4回起きた:

        08-26 出走表と予測が全消し → タップしても選手も確率も出ない
        08-26/27 採番の食い違いで昼から買い目生成が停止
        08-28 判定が途中で切れて1日ぶんDBに入らず
        08-29 締切時刻とグレードが全消し → 買い目が確定しない

    消えた項目を1つずつ守る方式では追いつかなかった（entries → predictions →
    closing_time → grade と、そのつど別の項目が消えた）。

    そこで役割を分けた:

        クラウド … JSON を作る（データが揃っているのはこちらだけ）
        ローカル … JSON を読んで履歴DBへ取り込み、**結果だけ書き足す**

    この関数は行を消さず、項目を空にせず、採番も触らない。**増えるだけ**。
    だから履歴DBに何が欠けていても JSON を壊せない。

    実測（2026-08-27〜29）でクラウドの最終版は全行に is_hit と着順が入っており、
    bets 側はそもそも書き足す必要がない。races の着順だけがローカル由来。

    戻り値: 書き足した件数。
    """
    _ensure_data_dir()
    d = target_date
    races_path = DATA_DIR / f"races_{d}.json"
    bets_path = DATA_DIR / f"bets_{d}.json"
    filled = {"races_order": 0, "bets_order": 0, "bets_judged": 0}

    if not races_path.exists() and not bets_path.exists():
        # クラウドが1度も書いていない日。ここだけは作るしかない。
        logger.warning(f"export: {d} の JSON が無いので export_day で作ります"
                       f"（クラウドが動かなかった日）")
        return export_day(d)

    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race, RaceResult, Stadium

    with get_session() as session:
        rows = (
            session.query(Race, Stadium)
            .join(Stadium, Race.stadium_id == Stadium.id)
            .filter(Race.race_date == d).all()
        )
        # 突き合わせは場名とレース番号。JSON の採番は当てにしない（_race_key と同じ理由）
        key_of = {r.id: (s.name, r.race_no) for r, s in rows}
        order_by_key: dict[tuple, list[int]] = {}
        if key_of:
            for rid, _o, boat in (
                session.query(RaceResult.race_id, RaceResult.arrival_order,
                              RaceResult.boat_no)
                .filter(RaceResult.race_id.in_(list(key_of)))
                .order_by(RaceResult.race_id, RaceResult.arrival_order).all()
            ):
                if boat is not None:
                    order_by_key.setdefault(key_of[rid], []).append(int(boat))

        judged_by_key: dict[tuple, tuple] = {}
        for b, r, s in (
            session.query(Bet, Race, Stadium)
            .join(Race, Bet.race_id == Race.id)
            .join(Stadium, Race.stadium_id == Stadium.id)
            .filter(Race.race_date == d, Bet.is_hit != None).all()   # noqa: E711
        ):
            judged_by_key[(s.name, r.race_no, b.bet_type, b.combination)] = (
                bool(b.is_hit), b.actual_payout)

    if races_path.exists() and order_by_key:
        races = json.loads(races_path.read_text(encoding="utf-8"))
        for r in races:
            if r.get("result_order"):
                continue
            order = order_by_key.get((r.get("stadium"), r.get("race_no")))
            if order:
                r["result_order"] = order
                filled["races_order"] += 1
        if filled["races_order"]:
            races_path.write_text(json.dumps(races, ensure_ascii=False, indent=None),
                                  encoding="utf-8")

    if bets_path.exists():
        bets = json.loads(bets_path.read_text(encoding="utf-8"))
        changed = False
        for b in bets:
            rkey = (b.get("stadium_name"), b.get("race_no"))
            if not b.get("result_order") and order_by_key.get(rkey):
                b["result_order"] = order_by_key[rkey]
                filled["bets_order"] += 1
                changed = True
            if b.get("is_hit") is None:
                got = judged_by_key.get((*rkey, b.get("bet_type"), b.get("combination")))
                if got is not None:
                    b["is_hit"], b["actual_payout"] = got
                    filled["bets_judged"] += 1
                    changed = True
        if changed:
            bets_path.write_text(json.dumps(bets, ensure_ascii=False, indent=None),
                                 encoding="utf-8")

    logger.info(
        f"export: {d} の結果を書き足しました（races着順 {filled['races_order']} / "
        f"bets着順 {filled['bets_order']} / bets判定 {filled['bets_judged']}）")
    return filled


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
        # races/bets と同じ計算値にする（json_race_id 参照）。
        # ここが DB 採番のままだと refresh_odds が races と噛み合わない。
        entry["race_id"] = json_race_id(target_date, stadium_code, race_no) or race_id
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

    # 中身のある記録を減らして上書きしない（bets / races と同じ手当て）。
    # probs はクラウドの使い捨てDBで作られ、その日の全レースぶんの予測が入る。
    # 履歴DBには予測が無い（2026-08-23 以降）ので、ローカルでここを走らせると
    # 買い目のあるレースだけに縮む。2026-08-29 に手で実行して 144→36 レースに
    # 潰したのを確認した。probs が欠けると refresh_odds がそのレースの買い目を
    # 一切作れなくなる。レースは日中に減らないので、減る＝壊れている。
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8")).get("races", [])
        except Exception:
            prev = []
        if len(prev) > len(race_map):
            logger.warning(
                f"export: {path.name} は {len(prev)}→{len(race_map)}レースに"
                f"減るため書き換えません")
            return

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
