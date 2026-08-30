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
from datetime import date
from itertools import combinations, permutations
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

D = date(2026, 9, 1)
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
    from src.ingestion.models import (Race, RaceEntry, RaceResult, Payout,
                                      Stadium, Prediction)
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
