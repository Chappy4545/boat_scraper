"""日次パイプラインを**通しで**動かす。単体テストでは捕まらない層。

なぜ要るか
--------
2026-08-30 までの1週間で見つかったバグは14件。**ほぼ全部が
「単体テストは通るが、繋ぐと壊れる」種類**だった:

    クラウドが書いた JSON をローカルの判定が空で上書きする
    probs と races で race_id の体系が違い、買い目が作れない
    記録だけのはずの賭式に賭け金が付く
    1レース上限5本のままで賭式が丸ごと落ちる
    払戻の重複で DB 保存が全滅する（終了コードは0）

当時テストは270件あったが**全部が単体**で、ファイルをまたぐ経路を
1つも通していなかった。だから毎回ユーザーが画面を見て気づくまで
分からなかった。ここではその経路そのものを動かす。

やること
------
一時ディレクトリに configs / docs/data を用意し、使い捨てDBを作って
実際のコマンドを呼ぶ。ネットにだけ触らせない（スクレイパを差し替える）。

    朝: クラウドが probs / races / bets を書く（export_day / export_probs）
    昼: refresh_odds が板を見て買い目を作り直す
    夜: ローカルが結果を書き足す（fill_results_into_json）

そのうえで「壊れていたら必ず落ちる」不変条件を確認する。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import date, datetime, time as dt_time, timedelta
from itertools import combinations, permutations
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ⚠️ 固定日にしてはいけない。買い目の生成は「締切を過ぎたレースには賭け金を
# 付けない」ので、日付を固定すると**その日を過ぎた瞬間に全テストが 0 件になる**。
# 実際 2026-08-31 に書いた `date(2026, 9, 1)` は 9/2 から 8 本落ち始めた
# （締切 11:30 / 12:30 が常に過去になるため）。翌日を使えば常に締切前。
D = date.today() + timedelta(days=1)
STADIUM = ("01", "桐生")
RACES = (1, 2)          # 2レースで十分（経路を通すのが目的）


# ── ネットの代わり ────────────────────────────────

def _mk(bet_type, combos, base):
    return pd.DataFrame([{"bet_type": bet_type, "combination": c,
                          "odds": round(base + i * 0.7, 1)}
                         for i, c in enumerate(combos)])


class FakeScraper:
    """全賭式のオッズを返す。通信しない。"""

    def __init__(self, config=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    _B = [1, 2, 3, 4, 5, 6]

    def get_odds_tansho(self, *a):
        return _mk("tansho", [str(b) for b in self._B], 1.5)

    def get_odds_fukusho(self, *a):
        return _mk("fukusho", [str(b) for b in self._B], 1.1)

    def get_odds_kakurenfuku(self, *a):
        return _mk("kakurenfuku", [f"{x}-{y}" for x, y in combinations(self._B, 2)], 1.4)

    def get_odds_nirenfuku(self, *a):
        return _mk("nirenfuku", [f"{x}-{y}" for x, y in combinations(self._B, 2)], 2.0)

    def get_odds_nirentan(self, *a):
        return _mk("nirentan", [f"{x}-{y}" for x, y in permutations(self._B, 2)], 3.0)

    def get_odds_sanrenfuku(self, *a):
        return _mk("sanrenfuku", [f"{x}-{y}-{z}" for x, y, z in combinations(self._B, 3)], 4.0)

    def get_odds_sanrentan(self, *a):
        return _mk("sanrentan", [f"{x}-{y}-{z}" for x, y, z in permutations(self._B, 3)], 8.0)

    # judge_live は「終了済みレース」を払戻一覧ページから特定する。
    #
    # ⚠️ 差し替えるのは**ネットワークとページ解析まで**。ここで検証したいのは
    # 解析結果を受け取った後の組み立て（判定漏れ・行の消失・別レースへの混入）で、
    # HTMLの読み方ではない。今週のバグ14件は1件もパーサに無かった。
    # パーサが壊れた場合は「収集0件」として現れ、daily_check が担当する。
    # 実HTMLを固定資産にする案は採らない。サイトが変われば古い雪だるまに対して
    # 通り続け、本番が壊れていても緑になる（＝偽の安心）。
    #
    # **終了しているのは1レース目だけ**にする。日中はこれが普通の状態で、
    # 「まだ終わっていないレースの買い目を壊さないか」を見たいため。
    FINISHED = (RACES[0],)

    def _url(self, key):
        return f"http://fake/{key}"

    def _fetch_raw(self, url, params=None):
        return "<html></html>"

    def parse_pay_summary(self, html):
        return [(STADIUM[0], rn) for rn in self.FINISHED]

    # cmd_predict_cloud と _catchup_missed_results はここを通る。
    # 「その日ぶんの出走表・オッズ・結果」をまとめて返す入口。
    def collect_day(self, d, max_workers=5, skip_before_info=True,
                    skip_odds=False, skip_results=False):
        entries = pd.DataFrame([
            {"stadium_code": STADIUM[0], "race_date": d, "race_no": rn,
             "boat_no": b, "racer_no": 1000 + b, "racer_name": f"選手{b}",
             "racer_class": "A1", "age": 30, "weight": 52.0,
             "f_count": 0, "l_count": 0, "avg_st": 0.16,
             "motor_no": b, "boat_no_equipment": b,
             "national_win_rate": 6.0 - b * 0.3, "national_top2_rate": 40.0,
             "national_top3_rate": 60.0, "local_win_rate": 6.0 - b * 0.3,
             "local_top2_rate": 40.0, "local_top3_rate": 60.0,
             "motor_top2_rate": 40.0, "motor_top3_rate": 60.0,
             "boat_top2_rate": 38.0, "boat_top3_rate": 58.0,
             "grade": "一般", "race_type": "予選", "is_night": False,
             "closing_time": f"1{rn}:30"}
            for rn in RACES for b in self._B])

        def _odds(name, getter):
            frames = []
            for rn in RACES:
                df = getter().copy()
                df["stadium_code"] = STADIUM[0]
                df["race_date"] = d
                df["race_no"] = rn
                frames.append(df)
            return pd.concat(frames, ignore_index=True)

        out = {
            "racelist": entries,
            "odds_tansho": _odds("tansho", self.get_odds_tansho),
            "odds_nirenfuku": _odds("nirenfuku", self.get_odds_nirenfuku),
            "odds_nirentan": _odds("nirentan", self.get_odds_nirentan),
            "odds_sanrenfuku": _odds("sanrenfuku", self.get_odds_sanrenfuku),
            "odds_sanrentan": _odds("sanrentan", self.get_odds_sanrentan),
        }
        if not skip_results:
            rr, py = [], []
            for rn in RACES:
                r1, p1 = self.get_race_result_and_payouts(STADIUM[0], d, rn)
                r1 = r1.copy(); r1["stadium_code"] = STADIUM[0]
                r1["race_date"] = d; r1["race_no"] = rn
                r1["racer_no"] = [1000 + b for b in r1["boat_no"]]
                r1["entry_course"] = r1["boat_no"]
                p1 = p1.copy(); p1["stadium_code"] = STADIUM[0]
                p1["race_date"] = d; p1["race_no"] = rn
                rr.append(r1); py.append(p1)
            out["race_result"] = pd.concat(rr, ignore_index=True)
            out["payouts"] = pd.concat(py, ignore_index=True)
        return out

    # 着順は 1,2,3,4,5,6 の順で固定（fixture の払戻と揃える）
    def get_race_result_and_payouts(self, stadium_code, race_date, race_no):
        rr = pd.DataFrame([{"boat_no": b, "arrival_order": o}
                           for o, b in enumerate(self._B, start=1)])
        py = pd.DataFrame([
            {"bet_type": "tansho", "combination": "1", "payout": 170},
            {"bet_type": "複勝", "combination": "1", "payout": 110},
            {"bet_type": "複勝", "combination": "2", "payout": 130},
            {"bet_type": "kakurenfuku", "combination": "1-2", "payout": 120},
            {"bet_type": "kakurenfuku", "combination": "1-3", "payout": 150},
            {"bet_type": "kakurenfuku", "combination": "2-3", "payout": 210},
            {"bet_type": "nirenfuku", "combination": "1-2", "payout": 260},
            {"bet_type": "sanrenfuku", "combination": "1-2-3", "payout": 480},
            {"bet_type": "sanrentan", "combination": "1-2-3", "payout": 990},
        ])
        return rr, py


# ── 一日ぶんの環境 ────────────────────────────────

@pytest.fixture
def day(tmp_path, monkeypatch):
    """一時ディレクトリに configs / docs/data / 使い捨てDB を用意する。"""
    (tmp_path / "configs").mkdir()
    shutil.copy(ROOT / "configs" / "config.yaml", tmp_path / "configs" / "config.yaml")
    (tmp_path / "docs" / "data").mkdir(parents=True)
    # 本番のモデルをそのまま使う。無いと予測が走らず経路を通せない
    # （モデルの中身は読むだけで書き換えない）
    shutil.copytree(ROOT / "data" / "processed" / "models",
                    tmp_path / "data" / "processed" / "models")
    shutil.copy(ROOT / "docs" / "data" / "stadiums.json",
                tmp_path / "docs" / "data" / "stadiums.json")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOAT_DB_URL", f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    monkeypatch.setattr("src.scraping.official.BoatRaceScraper", FakeScraper)

    from src.ingestion.database import init_db, get_session
    from src.ingestion.models import (BeforeInfo, Race, RaceEntry, RaceResult,
                                      Payout, Stadium, Prediction)
    from src.utils.helpers import load_config
    import src.export as E
    monkeypatch.setattr(E, "DATA_DIR", tmp_path / "docs" / "data")
    init_db(load_config())

    code, name = STADIUM
    with get_session() as s:
        s.add(Stadium(id=1, code=code, name=name))
    with get_session() as s:
        for i, rn in enumerate(RACES, start=1):
            s.add(Race(id=i, race_date=D, stadium_id=1, race_no=rn,
                       closing_time=f"1{rn}:30", grade="一般",
                       race_type="予選", is_night=False))
    with get_session() as s:
        for i, _rn in enumerate(RACES, start=1):
            for b in range(1, 7):
                s.add(RaceEntry(race_id=i, boat_no=b, racer_no=1000 + b,
                                racer_name=f"選手{b}", racer_class="A1",
                                age=30, weight=52.0, f_count=0, l_count=0,
                                avg_st=0.16, motor_no=b, boat_no_equipment=b,
                                national_win_rate=6.0 - b * 0.3,
                                national_top2_rate=40.0, national_top3_rate=60.0,
                                local_win_rate=6.0 - b * 0.3,
                                local_top2_rate=40.0, local_top3_rate=60.0,
                                motor_top2_rate=40.0, motor_top3_rate=60.0,
                                boat_top2_rate=38.0, boat_top3_rate=58.0))
                s.add(Prediction(race_id=i, model_version="v1", boat_no=b,
                                 win_prob=0.4 - b * 0.05, top2_prob=0.6 - b * 0.05,
                                 top3_prob=0.8 - b * 0.05, confidence=0.4))
    # 着順と払戻（1着=1, 2着=2, 3着=3）
    with get_session() as s:
        for i, _rn in enumerate(RACES, start=1):
            for order, boat in enumerate([1, 2, 3, 4, 5, 6], start=1):
                s.add(RaceResult(race_id=i, boat_no=boat, arrival_order=order,
                                 racer_no=1000 + boat, entry_course=boat))
    # 直前情報。⚠️ **着順と一緒に入れる。** 本番では夜の collect_day_results が
    # 両方まとめて集めるので、着順だけあって直前情報が無い日は本番に存在しない。
    # 片方だけ入れると「テスト環境が本番より綺麗／汚い」状態になり、
    # 2026-09-03 に追加した充足率の検査が合成日で誤発火した。
    with get_session() as s:
        for i, _rn in enumerate(RACES, start=1):
            for boat in range(1, 7):
                s.add(BeforeInfo(race_id=i, boat_no=boat, entry_course=boat,
                                 exhibition_time=6.70 + boat * 0.01, tilt=0.0))
    with get_session() as s:
        for i, _rn in enumerate(RACES, start=1):
            for bt, cb, pay in (("tansho", "1", 170), ("複勝", "1", 110),
                                ("複勝", "2", 130), ("nirenfuku", "1-2", 260),
                                ("kakurenfuku", "1-2", 120), ("kakurenfuku", "1-3", 150),
                                ("kakurenfuku", "2-3", 210),
                                ("sanrenfuku", "1-2-3", 480),
                                ("sanrentan", "1-2-3", 990)):
                s.add(Payout(race_id=i, bet_type=bt, combination=cb, payout=pay))
    return tmp_path


def _load(day_dir, name):
    p = day_dir / "docs" / "data" / f"{name}_{D}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _mark_final(day_dir):
    """買い目を「締切前に確定した」状態にする。

    _sync_bets_from_json は確定フラグが1つも無いと即 return する
    （クラウドが動かなかった日に朝のリストで上書きしないため）。
    テストの races は未来日なので refresh_odds では確定しない。
    ここで検証したいのは**突き合わせのロジック**であって確定のタイミングでは
    ないので、フラグだけ立てて同期を通す。
    ⚠️ 2026-08-31 に、これを立てずに書いたため夜のテストが同期処理に
    一度も到達しておらず、08-23 の「別の日へ116件挿入」を再現しても
    素通りした。
    """
    p = day_dir / "docs" / "data" / f"bets_{D}.json"
    bets = json.loads(p.read_text(encoding="utf-8"))
    for b in bets:
        b["is_final_pick"] = True
    p.write_text(json.dumps(bets, ensure_ascii=False), encoding="utf-8")
    return len(bets)


def _cfg_types():
    from src.models.plackett_luce import BET_TYPE_JP
    from src.utils.helpers import load_config
    c = load_config()["betting"]
    return ({BET_TYPE_JP[t] for t in c["bet_types"]},
            {BET_TYPE_JP[t] for t in c.get("paper_bet_types", [])})


# ── 朝: クラウドが書く ────────────────────────────

def test_朝の書き出しで全賭式のprobsが出る(day):
    """1本も買わないレースで賭式が落ちていないこと（08-30に単勝が27/168だった）。"""
    import main
    from src.export import export_day, export_probs
    main.cmd_predict(D)          # 予測→買い目→export_day/export_probs
    probs = _load(day, "probs")
    assert probs, "probs が書かれていない"
    got = {c["bet_type"] for e in probs["races"] for c in e["combinations"]}
    buy, paper = _cfg_types()
    missing = (buy | paper) - got
    assert not missing, f"probs に無い賭式: {missing}"


def test_朝のracesに締切時刻と出走表と予測がある(day):
    import main
    main.cmd_predict(D)
    races = _load(day, "races")
    assert races
    for r in races:
        assert r.get("closing_time"), "締切時刻が無い（買い目が確定しなくなる）"
        assert r.get("entries"), "出走表が無い"
        assert r.get("predictions"), "予測が無い"


# ── 昼: refresh_odds ─────────────────────────────

def test_昼の更新で全賭式の買い目が出る(day):
    import main
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    bets = _load(day, "bets")
    assert bets, "買い目が書かれていない"
    buy, paper = _cfg_types()
    got = {b["bet_type"] for b in bets}
    missing = (buy | paper) - got
    assert not missing, f"買い目に無い賭式: {missing}（上限本数で切られていないか）"


def test_賭け金が付くのは買う賭式だけ(day):
    """⚠️ 08-30 に実際に起きた。記録だけのはずの3連単に500円が付いていた。"""
    import main
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    buy, _paper = _cfg_types()
    bad = [b for b in _load(day, "bets")
           if (b.get("recommended_amount") or 0) > 0 and b["bet_type"] not in buy]
    assert not bad, f"記録だけの賭式に賭け金が付いている: {[b['bet_type'] for b in bad]}"


def test_締切を過ぎたレースに賭け金が付かない(day, monkeypatch):
    """⚠️ 2026-08-31 発見。朝のクラウド実行が最速レースの締切に間に合わず、
    締切の46分後・20分後に初めて現れた買い目に 500円 が付いていた。

    08-20 以降12日で24本（金額つきの7%）。買えなかったのに損益に入り、
    回収率を 1.3pt 下振れさせていた（日単位では 118% と 131% ほど違う）。

    fixture の締切は R1=11:30 / R2=12:30。R1 だけ過ぎた時刻で走らせる。

    ⚠️ 素の fixture では通せない。理由が2つある:
      1. DB にオッズが無いと買い目が全部「オッズなし」で先に見送られ、
         賭け金を決める段階まで到達しない
         → オッズが DB に入る cmd_predict_cloud で走らせる
      2. 合成データでは EV が本番の閾値(1.2)を越えず、やはり到達しない
         → **この検証だけ**閾値を緩める
    どちらも「経路に到達していないのに通る」空振りの原因になる。
    """
    import main
    import yaml
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race

    cfg_path = day / "configs" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["betting"].update({"min_expected_value": 0.1,
                           "min_model_confidence": 0.0, "min_odds": 1.0})
    cfg["betting"].pop("bet_type_overrides", None)
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    real_closed = main._closed_race_ids

    def fake(d, closing_of, now=None):
        # 「R1 の締切は過ぎ、R2 はこれから」の時刻を渡す
        return real_closed(d, closing_of,
                           now=datetime.combine(D, dt_time(12, 0), tzinfo=main.JST))

    monkeypatch.setattr(main, "date", _FixedDate)
    monkeypatch.setattr(main, "_closed_race_ids", fake)
    _release_db()
    main.cmd_predict_cloud(D, max_workers=1)

    # ⚠️ セッションの外で ORM の属性を読むと DetachedInstanceError。
    # 中で素の値へ落としてから出す。
    with get_session() as s:
        rows = [(r.race_no, b.bet_type, b.recommended_amount or 0, b.pass_reason)
                for b, r in (s.query(Bet, Race)
                             .join(Race, Bet.race_id == Race.id)
                             .filter(Race.race_date == D).all())]

    paid_closed = [x for x in rows if x[0] == RACES[0] and x[2] > 0]
    assert not paid_closed, \
        f"締切済みの R{RACES[0]} に賭け金が付いている: {paid_closed[:5]}"

    # 見送りの理由が「締切後」で残っていること（予算超過と区別する）
    reasons = {x[3] for x in rows if x[0] == RACES[0]}
    assert "締切後" in reasons, f"見送り理由が残っていない: {reasons}"


def test_朝にオッズが無くても昼の更新で買い目が出る(day, monkeypatch):
    """⚠️ 朝の実行を早める判断が、これが成り立つかに懸かっている。

    morning_predict は 09:30 JST に走る。08:00 でないのは「8時台は単勝
    オッズがまだ公開されていないレースが多い」ため（2026-08-21 実測:
    09:03 の取得で35%が0）。代わりにモーニングレース（締切08:30頃）を
    毎日捨てている。

    だが特徴量に単勝オッズは入っていない（FEATURE_COLS に無い）ので、
    朝の時点でオッズが無くても**予測そのものは作れる**。あとは
    refresh_odds が日中に板を見て買い目を作り直せばよい。
    ここではその「作り直し」が本当に効くかを確かめる。
    """
    import main
    from src.ingestion.database import get_session
    from src.ingestion.models import Odds

    monkeypatch.setattr(main, "date", _FixedDate)
    _release_db()
    main.cmd_predict_cloud(D, max_workers=1)

    with get_session() as s:            # 朝はオッズが無かったことにする
        for o in s.query(Odds).all():
            s.delete(o)
    main.cmd_predict(D)                 # オッズ無しで予測をやり直す

    probs = _load(day, "probs")
    assert probs and len(probs["races"]) == len(RACES), \
        "オッズが無いと予測(probs)まで作れなくなっている"
    before = [b for b in (_load(day, "bets") or [])
              if (b.get("recommended_amount") or 0) > 0]

    main.cmd_refresh_odds(D, max_workers=1)   # 日中の更新で板を見る

    after = _load(day, "bets") or []
    assert after, "更新後も買い目が1本も無い"
    types = {b["bet_type"] for b in after}
    buy, paper = _cfg_types()
    assert (buy | paper) <= types, f"賭式が欠けている: {types}"
    assert len(after) > len(before), \
        f"朝オッズ無し({len(before)}本) → 更新後({len(after)}本) で増えていない"


def test_買い目には必ずルール名が入る(day):
    """経路によって rule が付いたり付かなかったりしないこと。

    refresh_odds は "r5"/"record" を書くが、export_day は候補ルールの行に
    しか書いておらず、買う買い目は rule が無い（null）だった。同じ買い目が
    経路で別の形になる。2026-08-31 の実データにも rule=null が2本あった。
    突き合わせ自体は bet_key の `rule or "r5"` が吸収していたが、
    rule で束ねる集計は割れる。

    ⚠️ **買う買い目(is_pass=0)を DB に入れてから測ること。** 合成データの
    DB は全部が見送りで、そのままだと旧実装でも rule が埋まってしまい
    テストが素通りする（2026-08-31 に一度素通りさせた）。
    """
    import main
    from src.export import export_day
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)

    missing = [b for b in _load(day, "bets") if not b.get("rule")]
    assert not missing, f"refresh_odds が書いた買い目 {len(missing)}本に rule が無い"

    with get_session() as s:                # 買う買い目を DB に用意する
        rid = s.query(Race).filter(Race.race_date == D).first().id
        s.add(Bet(race_id=rid, model_version="v1",
                  bet_type="nirenfuku", combination="1-2",
                  model_prob=0.4, odds=2.6, expected_value=1.04,
                  recommended_amount=500, is_pass=False, is_final_pick=True))
    export_day(D)
    bets = _load(day, "bets")
    bought = [b for b in bets if (b.get("recommended_amount") or 0) > 0]
    assert bought, "前提: 買う買い目が書き出されている"
    missing = [b for b in bets if not b.get("rule")]
    assert not missing, \
        f"export_day が書いた買い目 {len(missing)}本に rule が無い"


def test_賭式ごとに1レース1点まで(day):
    import main
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    seen = {}
    for b in _load(day, "bets"):
        if b.get("rule") in ("r5", "record"):
            k = (b["race_id"], b["bet_type"])
            seen[k] = seen.get(k, 0) + 1
    over = {k: v for k, v in seen.items() if v > 1}
    assert not over, f"同じレース・同じ賭式で複数出ている: {over}"


def test_買い目からレースを引ける(day):
    """画面のタップ。race_id の体系がズレると空振りする。"""
    import main
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    rid = {r["id"] for r in _load(day, "races")}
    lost = [b for b in _load(day, "bets") if b["race_id"] not in rid]
    assert not lost, f"{len(lost)}件がレースを引けない"


# ── 夜: ローカルが結果を書き足す ──────────────────────

def test_夜の判定でracesの中身が消えない(day):
    """⚠️ 08-26/29 に実際に起きた。出走表・予測・締切時刻が全消しになった。"""
    import main
    from src.export import fill_results_into_json
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    before = _load(day, "races")
    n_bets = len(_load(day, "bets"))

    fill_results_into_json(D)

    after = _load(day, "races")
    assert len(after) == len(before)
    for a, b in zip(before, after):
        for k in ("closing_time", "entries", "predictions", "grade"):
            assert b.get(k), f"{k} が消えた"
    assert len(_load(day, "bets")) == n_bets, "買い目の行が減った"
    assert all(r.get("result_order") for r in after), "着順が書き足されていない"


# ── DB保存 ──────────────────────────────────────

def test_払戻が重複しても落ちない(day):
    """⚠️ 08-30 に2回発生。UNIQUE違反でその日の保存が全滅し、終了コードは0だった。

    セッションは autoflush=False なので、直前に add した行は
    重複チェックのクエリに引っかからない。
    """
    from src.ingestion.saver import save_payouts
    df = pd.DataFrame([
        {"stadium_code": STADIUM[0], "race_date": D, "race_no": RACES[0],
         "bet_type": "sanrentan", "combination": "5-1-3", "payout": 10400},
        {"stadium_code": STADIUM[0], "race_date": D, "race_no": RACES[0],
         "bet_type": "sanrentan", "combination": "5-1-3", "payout": 10400},
    ])
    n = save_payouts(df)          # 落ちないこと
    assert n >= 1
    from src.ingestion.database import get_session
    from src.ingestion.models import Payout
    with get_session() as s:
        rows = s.query(Payout).filter_by(bet_type="sanrentan",
                                         combination="5-1-3").all()
        assert len(rows) == 1, "重複が2行入っている"


# ── 夜: ローカルDBが欠けている現実を再現する ──────────────
#
# ⚠️ ここが 2026-08-26 / 08-29 の事故の本体。
# テスト用DBを完璧に作ってしまうと、export_day を呼んでも何も壊れず
# **バグが素通りする**（実際 08-30 に書いた最初の版がそうだった）。
# 本番のローカル履歴DBは中身が欠けている:
#     予測が無い（08-23 に予測をクラウドの仕事にした）
#     出走表が無い日がある（結果だけ collect した日）
#     締切時刻・グレードが無い日がある（同上）
# その状態を作ってから夜の処理を通す。

def _strip_local_db():
    """履歴DBの実態にする（予測・出走表・締切時刻・グレードを消す）。"""
    from src.ingestion.database import get_session
    from src.ingestion.models import Prediction, RaceEntry, Race
    with get_session() as s:
        s.query(Prediction).delete()
        s.query(RaceEntry).delete()
        for r in s.query(Race).all():
            r.closing_time = None
            r.grade = None


def test_欠けたDBで夜を通してもracesが壊れない(day):
    """⚠️ 08-26 は出走表と予測、08-29 は締切時刻とグレードが全消しになった。"""
    import main
    from src.export import fill_results_into_json
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    before = _load(day, "races")
    n_bets = len(_load(day, "bets"))

    _strip_local_db()               # ← ここから先はローカル履歴DBの状態
    fill_results_into_json(D)

    after = _load(day, "races")
    assert len(after) == len(before)
    for a, b in zip(before, after):
        for k in ("closing_time", "entries", "predictions", "grade"):
            assert b.get(k), f"{k} が消えた（DBに無いものを null で上書きしている）"
    assert len(_load(day, "bets")) == n_bets, "買い目の行が減った"


def test_欠けたDBで夜を通しても買い目の金額が変わらない(day):
    """買い目はクラウドの記録が正。ローカルのDB都合で書き換えない。"""
    import main
    from src.export import fill_results_into_json
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    before = {(b["race_id"], b["bet_type"], b["combination"]):
              (b.get("recommended_amount") or 0) for b in _load(day, "bets")}

    _strip_local_db()
    fill_results_into_json(D)

    after = {(b["race_id"], b["bet_type"], b["combination"]):
             (b.get("recommended_amount") or 0) for b in _load(day, "bets")}
    assert after == before, "夜の処理で買い目の金額が変わった"


# ── クラウドの日中判定（judge_live）──────────────────

def test_日中判定は終わったレースだけを判定する(day):
    """クラウドが15分ごとに走らせる経路。

    終了しているのは1レース目だけ。**まだ終わっていない2レース目の買い目を
    判定したり消したりしない**こと。締切後に買い目が生える不具合
    （2026-08-12: 18本中5本が締切後に生成）と裏返しの関係にある。
    """
    import main
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    main.cmd_judge_live(D, max_workers=1)

    bets = _load(day, "bets")
    assert bets
    by_race = {}
    for b in bets:
        by_race.setdefault(b["race_no"], []).append(b)
    done, pending = FakeScraper.FINISHED[0], RACES[1]

    assert all(b.get("is_hit") is not None for b in by_race[done]),         "終わったレースが未判定のまま"
    assert all(b.get("result_order") for b in by_race[done]), "着順が入っていない"
    assert all(b.get("is_hit") is None for b in by_race[pending]),         "まだ終わっていないレースを判定している"

    # 1着=1, 2着=2 なので 2連複 1-2 は当たり
    nf = [b for b in by_race[done] if b["bet_type"] == "nirenfuku"]
    for b in nf:
        assert b["is_hit"] == (b["combination"] == "1-2"),             f"判定が着順と合わない: {b['combination']} -> {b['is_hit']}"


def test_日中判定は買い目の行を減らさない(day):
    """08-23 に判定のたび JSON が縮んだ（44件→29件）。"""
    import main
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    before = len(_load(day, "bets"))
    main.cmd_judge_live(D, max_workers=1)
    assert len(_load(day, "bets")) == before, "判定で行が減った"


# ── 夜の判定（cmd_judge。DBへの取り込みを含む）────────────

def test_夜の判定でJSONの買い目がDBに入る(day):
    """⚠️ _sync_bets_from_json は 08-23 に **別の日のレースへ116件挿入**した。

    JSON の race_id はクラウドの使い捨てDB採番なので、突き合わせに使うと
    まったく別のレースに入る。場名とレース番号で引き直すのが正しい。

    ⚠️ 判定条件に注意。「DBの件数 >= JSONの件数」では**壊れても通る**
    （DBには cmd_predict が作った行が元から大量にある）。2026-08-31 に
    実際それで素通りした。見るべきは次の2つ:
      1. どのレースにも紐づかない買い目が生まれていないか（宙に浮いた行）
      2. JSON に載っている買い目が「条件を外れた」扱いにされていないか
    """
    import main
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    main.cmd_judge_live(D, max_workers=1)
    n_json = _mark_final(day)
    assert n_json > 0

    main.cmd_judge(D)

    with get_session() as s:
        race_ids = {r.id for r in s.query(Race).all()}
        orphan = [b for b in s.query(Bet).all() if b.race_id not in race_ids]
        assert not orphan,             f"どのレースにも紐づかない買い目が {len(orphan)}件（別の日へ挿入する経路）"

        other_day = (s.query(Bet).join(Race, Bet.race_id == Race.id)
                     .filter(Race.race_date != D).count())
        assert other_day == 0, f"別の日のレースに {other_day}件 挿入された"

        dropped = (s.query(Bet).join(Race, Bet.race_id == Race.id)
                   .filter(Race.race_date == D,
                           Bet.pass_reason == "日中に条件を外れた").count())
        assert dropped == 0,             f"JSON に載っている買い目が {dropped}件「条件を外れた」にされた（突き合わせ失敗）"


def test_古いJSONを取り込んでも確定フラグを消さない(day):
    """⚠️ 2026-09-02 に確定12本が消えた（住之江5R・福岡11R の全6賭式）。

    ローカルは judge の前に pull するが、その後 ingest_odds と
    collect_results で約9分かかる。クラウドは15分ごとに書くので、
    **その隙間に確定させた分**を持たない JSON を読むことがある:

        16:45 ローカルが pull（住之江5R はまだ未確定）
        16:49 クラウドが確定させて commit   ← pull より後
        16:54 ローカルの judge が 16:45 の版を読んで export → 確定が消えた

    取り込む JSON が古いこと自体は避けきれない。だから確定フラグを
    **false→true の一方通行**にして、古い版を読んでも壊れないようにする。
    """
    import main
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    _mark_final(day)
    main.cmd_judge(D)

    with get_session() as s:
        n_before = (s.query(Bet).join(Race, Bet.race_id == Race.id)
                    .filter(Race.race_date == D, Bet.is_final_pick.is_(True)).count())
    assert n_before > 0, "前提: 確定した買い目がDBにある"

    # クラウドが確定させる前の「古い版」を読ませる
    p = day / "docs" / "data" / f"bets_{D}.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    stale = [{**b, "is_final_pick": (i == 0)} for i, b in enumerate(rows)]
    p.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    main.cmd_judge(D)

    with get_session() as s:
        n_after = (s.query(Bet).join(Race, Bet.race_id == Race.id)
                   .filter(Race.race_date == D, Bet.is_final_pick.is_(True)).count())
    assert n_after >= n_before, (
        f"古い JSON を読んで確定フラグが {n_before}→{n_after} に減った")


def test_夜の判定で記録用の賭式が損益に入らない(day):
    """買っていないものを損益に混ぜない。is_pass で区別する。"""
    import main
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    main.cmd_judge_live(D, max_workers=1)
    _mark_final(day)
    main.cmd_judge(D)

    buy, _paper = _cfg_types()
    with get_session() as s:
        bad = [b for b in (s.query(Bet).join(Race, Bet.race_id == Race.id)
                           .filter(Race.race_date == D).all())
               if not b.is_pass and b.bet_type not in buy]
        assert not bad, f"記録用の賭式が損益に入っている: {[b.bet_type for b in bad]}"


# ── 朝: クラウドが使い捨てDBで予測を作る（cmd_predict_cloud）──────
# 毎朝ここが動いて一日が始まる。ここが黙って何もしないと買い目が0本になる。

def test_クラウドの朝の予測が一日ぶんを書き出す(day, monkeypatch):
    """履歴DBなしで probs / races / オッズ退避まで揃うこと。"""
    import main
    monkeypatch.setattr(main, "date", _FixedDate)      # 「今日」を D にする
    _release_db()
    main.cmd_predict_cloud(D, max_workers=1)

    races = _load(day, "races")
    probs = _load(day, "probs")
    assert races, "races が書かれていない"
    assert len(races) == len(RACES), f"レース数 {len(races)}"
    for r in races:
        assert r["entries"], f"R{r['race_no']} の出走表が空"
        assert r["predictions"], f"R{r['race_no']} の予測が空"
        assert r["closing_time"], f"R{r['race_no']} の締切時刻が無い"

    buy, paper = _cfg_types()
    got = {c["bet_type"] for e in probs["races"] for c in e["combinations"]}
    assert (buy | paper) <= got, f"賭式が欠けている: 出た={got}"

    # オッズは過去日に遡れない。ここで退避しないと永久に失われる。
    gz = day / "docs" / "data" / f"odds_raw_{D}.json.gz"
    assert gz.exists() and gz.stat().st_size > 0, "オッズを退避していない"


def test_クラウドの朝は始まった日を作り直さない(day, monkeypatch):
    """⚠️ 日中に呼ばれても、その日の記録を消してはいけない。

    GitHub の schedule はベストエフォートで、実測15〜44分遅れる。遅れて
    昼に動くと `cmd_predict` が買い目を入れ直し、確定した買い目・判定結果を
    消す。それらは JSON にしか無い日がある。
    """
    import main
    monkeypatch.setattr(main, "date", _FixedDate)
    _release_db()
    main.cmd_predict_cloud(D, max_workers=1)
    main.cmd_refresh_odds(D, max_workers=1)
    _mark_final(day)
    before = _load(day, "bets")
    assert before, "前提: 買い目がある"

    main.cmd_predict_cloud(D, max_workers=1)          # 2回目（遅れて起動）
    after = _load(day, "bets")
    assert len(after) == len(before), \
        f"確定済みの日を作り直した: {len(before)}本 → {len(after)}本"
    assert all(b["is_final_pick"] for b in after), "確定フラグが落ちている"


# ── 取りこぼしの穴埋め（_catchup_missed_results）──────────────
# PC が止まった日を後から埋める経路。⚠️ ここは過去に
# 「レース後に作り直した買い目が損益に混ざる」事故を起こしている。

def _make_catchup_target(day):
    """その日を穴埋めの対象にする（着順が無く、未判定の買う買い目がある）。

    ⚠️ これを作らないと、その日は穴埋めのどの枝にも入らず、テストは
    「何も起きなかったので通る」状態になる。2026-08-31 に実際に
    2本続けてこの空振りを書いた。**対象に入っていることまで確かめる。**
    """
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race, RaceResult
    with get_session() as s:
        for rr in s.query(RaceResult).all():
            s.delete(rr)
    with get_session() as s:
        rid = s.query(Race).filter(Race.race_date == D).first().id
        s.add(Bet(race_id=rid, model_version="v1",
                  bet_type="nirenfuku", combination="1-2",
                  model_prob=0.4, odds=2.6, expected_value=1.04,
                  recommended_amount=500, is_pass=False, is_final_pick=True,
                  is_hit=None))
    with get_session() as s:
        assert s.query(RaceResult).count() == 0
        assert s.query(Bet).filter(Bet.is_pass == False,          # noqa: E712
                                   Bet.is_hit.is_(None)).count() == 1


def test_穴埋めは当日の記録を作り直さない(day, monkeypatch):
    """⚠️ レース後に作った買い目は確定オッズで選ぶことになる。

    2026-08-17 の復旧で 08-16 の朝の予測（690件と probs）が今日づけの
    再予測で消えた。レース後に作り直した買い目は損益の集計から外れる
    （`date(created_at) <= race_date` の条件）ので、作り直すとその日の
    記録が丸ごと検証に使えなくなる。当日の記録があるなら着順を足すだけ。
    """
    import main
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race
    monkeypatch.setattr(main, "date", _FixedDate)
    _release_db()
    main.cmd_predict_cloud(D, max_workers=1)
    main.cmd_refresh_odds(D, max_workers=1)
    _mark_final(day)
    _make_catchup_target(day)           # ここを通らないとテストが空振りする

    picks_before = {(b["stadium_name"], b["race_no"], b["bet_type"],
                     b["combination"]) for b in _load(day, "bets")
                    if b.get("is_final_pick")}
    assert picks_before, "前提: 確定した買い目がある"
    with get_session() as s:
        ids_before = {b.id for b, _r in
                      s.query(Bet, Race).join(Race, Bet.race_id == Race.id)
                      .filter(Race.race_date == D).all()}
    assert len(ids_before) > 100, f"前提: 当日の買い目が DB にある({len(ids_before)})"

    monkeypatch.setattr(main, "date", _FixedDate.plus(1))
    main._catchup_missed_results(lookback_days=1, max_workers=1)

    # ⚠️ 件数の増減では見ない。cmd_judge が JSON の買い目を DB へ同期するので
    # 増えるのが正常。また created_at でも見ない —— DB の既定値は実時刻
    # (utcnow) なので、テストの日付を未来に置くと「レース後に作られた」が
    # 原理的に観測できない（2026-08-31 にこれで空振りした）。
    #
    # 見るのは**行が入れ替わっていないか**。cmd_predict はそのレースの
    # 買い目を model_version ごと削除して入れ直すので id が総取っ替えになる。
    # 同期(_sync_bets_from_json)は行を消さず is_pass を変えるだけなので、
    # 正常なら id は全部残る。
    with get_session() as s:
        ids_after = {b.id for b, _r in
                     s.query(Bet, Race).join(Race, Bet.race_id == Race.id)
                     .filter(Race.race_date == D).all()}
    lost = ids_before - ids_after
    assert not lost, (
        f"当日の買い目 {len(lost)}/{len(ids_before)}件 が作り直された。"
        f"レース後の再予測は確定オッズで選ぶことになり、損益の集計から外れる")

    picks_after = {(b["stadium_name"], b["race_no"], b["bet_type"],
                    b["combination"]) for b in _load(day, "bets")
                   if b.get("is_final_pick")}
    assert picks_before <= picks_after, \
        f"当日の確定買い目が消えた: {picks_before - picks_after}"



def test_穴埋めが買い目を別の日に入れない(day, monkeypatch):
    """⚠️ 2026-08-23 に116件が別の日づけで入り、損益に混ざった。"""
    import main
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race
    monkeypatch.setattr(main, "date", _FixedDate)
    _release_db()
    main.cmd_predict_cloud(D, max_workers=1)
    main.cmd_refresh_odds(D, max_workers=1)
    _mark_final(day)

    monkeypatch.setattr(main, "date", _FixedDate.plus(1))
    main._catchup_missed_results(lookback_days=1, max_workers=1)

    with get_session() as s:
        rows = (s.query(Bet, Race).join(Race, Bet.race_id == Race.id).all())
        other = [(b.id, str(r.race_date)) for b, r in rows if r.race_date != D]
        assert not other, f"{len(other)}件が別の日に入った: {other[:5]}"
        # レース日より後に作られた買い目は確定オッズで選んだことになる
        late = [b.id for b, r in rows
                if b.created_at and b.created_at.date() > r.race_date]
        assert not late, f"レース後に作られた買い目が {len(late)}件"


def test_穴埋めが丸ごと取り逃した日を埋める(day, monkeypatch):
    """⚠️ PC が止まった日。この枝が壊れていた頃に実際にデータを失っている。

    旧実装は `race_cnt > 0` を条件にしており、レースが0件の日は永久に
    対象外だった（2026-07-28〜31, 08-08〜09 が欠落）。

    ⚠️ **5日前**を使う。前日だと `bet_cnt == 0` の枝でも拾われてしまい、
    `race_cnt == 0` の枝を消しても通ってしまう（再収集の枝は直近3日だけ）。
    2026-08-31 に前日で書いてこの空振りを踏んだ。
    """
    import main
    from src.ingestion.database import get_session
    from src.ingestion.models import Race, RaceResult
    missed = D - timedelta(days=5)          # 再収集の窓(3日)より前

    with get_session() as s:
        assert s.query(Race).filter(Race.race_date == missed).count() == 0, \
            "前提: その日のレースが1件も無い"

    monkeypatch.setattr(main, "date", _FixedDate)   # 今日 = D
    main._catchup_missed_results(lookback_days=7, max_workers=1)

    with get_session() as s:
        n_race = s.query(Race).filter(Race.race_date == missed).count()
        n_res = (s.query(RaceResult).join(Race, RaceResult.race_id == Race.id)
                 .filter(Race.race_date == missed).count())
    assert n_race == len(RACES), f"レースを埋めていない: {n_race}件"
    assert n_res > 0, "着順を埋めていない"


def test_穴埋めが着順の無い日を判定できる状態にする(day, monkeypatch):
    """買い目はあるが着順が無い日＝judge の前に落ちた日。

    ⚠️ 買う買い目(is_pass=0)は cmd_predict では作られない。本番では
    cmd_judge が JSON から同期して初めて DB に入る。合成データでは
    EV の条件を満たす買い目が出ないことがあるので、ここでは検証したい
    状態（買う買い目があり着順が無い）を直接作る。
    2026-08-31 に、これを作らずに書いたため穴埋めがどの枝にも入らず、
    テストが素通りした。
    """
    import main
    from src.ingestion.database import get_session
    from src.ingestion.models import Bet, Race, RaceResult
    monkeypatch.setattr(main, "date", _FixedDate)
    _release_db()
    main.cmd_predict_cloud(D, max_workers=1)
    main.cmd_refresh_odds(D, max_workers=1)

    with get_session() as s:            # 着順を消して「judge 前に落ちた日」に
        for rr in s.query(RaceResult).all():
            s.delete(rr)
    with get_session() as s:
        rid = s.query(Race).filter(Race.race_date == D).first().id
        s.add(Bet(race_id=rid, model_version="v1",
                  bet_type="nirenfuku", combination="1-2",
                  model_prob=0.4, odds=2.6, expected_value=1.04,
                  recommended_amount=500, is_pass=False, is_final_pick=True,
                  is_hit=None))
    with get_session() as s:
        assert s.query(RaceResult).count() == 0, "前提: 着順が無い"
        assert s.query(Bet).filter(Bet.is_pass == False,          # noqa: E712
                                   Bet.is_hit.is_(None)).count() == 1, \
            "前提: 未判定の買う買い目がある"

    monkeypatch.setattr(main, "date", _FixedDate.plus(1))
    main._catchup_missed_results(lookback_days=1, max_workers=1)

    with get_session() as s:
        assert s.query(RaceResult).count() > 0, "着順を取り直していない"
        unjudged = (s.query(Bet).join(Race, Bet.race_id == Race.id)
                    .filter(Race.race_date == D, Bet.is_pass == False,   # noqa: E712
                            Bet.is_hit.is_(None)).count())
        assert unjudged == 0, f"判定されていない買い目が {unjudged}本"


def _release_db():
    """使い捨てDBのファイルを掴んだままにしない。

    cmd_predict_cloud は BOAT_DB_URL のDBを消してから作り直す（前日の残りを
    混ぜないため）。本番は新しいプロセスなので誰も掴んでいないが、テストは
    fixture が既に開いている。Windows は使用中のファイルを消せない。
    """
    import src.ingestion.database as db
    if db._engine is not None:
        db._engine.dispose()


class _FixedDate(date):
    """`main.date.today()` を固定する。

    cmd_predict_cloud / _catchup_missed_results は `date.today()` を見て
    「今日か過去日か」を変える。実時間に任せると、動かす日によって
    通ったり通らなかったりするテストになる。
    """

    _OFFSET = 0

    @classmethod
    def today(cls):
        return D + timedelta(days=cls._OFFSET)

    @classmethod
    def plus(cls, days):
        return type("_FixedDatePlus", (_FixedDate,), {"_OFFSET": days})


# ── 監視（daily_check）─────────────────────────────

def test_正常な一日なら点検が警報を出さない(day):
    """点検が誤警報を出すと、本物の異常が埋もれる。"""
    import main
    sys.path.insert(0, str(ROOT / "scripts"))
    from daily_check import json_integrity_checks
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    checks, _peak = json_integrity_checks(str(D), day / "docs" / "data")
    ng = [(n, t) for n, ok, t in checks if not ok]
    assert not ng, f"正常な日に警報: {ng}"


def test_racesを壊すと点検が捕まえる(day):
    """08-26/29 の壊れ方（中身が空）を点検が見逃さないこと。"""
    import main
    sys.path.insert(0, str(ROOT / "scripts"))
    from daily_check import json_integrity_checks
    main.cmd_predict(D)
    main.cmd_refresh_odds(D, max_workers=1)
    p = day / "docs" / "data" / f"races_{D}.json"
    races = json.loads(p.read_text(encoding="utf-8"))
    for r in races:
        r["entries"] = []
        r["closing_time"] = None
    p.write_text(json.dumps(races, ensure_ascii=False), encoding="utf-8")

    checks, _ = json_integrity_checks(str(D), day / "docs" / "data")
    ng = {n for n, ok, _ in checks if not ok}
    assert "出走表と予測" in ng, "出走表の全消しを見逃した"
    assert "締切時刻" in ng, "締切時刻の全消しを見逃した"
