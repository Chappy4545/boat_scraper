"""races JSON の出走表・予測が、書き出しのたびに消えていないかを見る。

2026-08-26 の事故
----------------
画面で買い目をタップしても選手名も確率も出ない、という報告から判明した。

  クラウド(predict_cloud)が 00:44 に書いた 168レース（entries/predictions とも
  168件）を、ローカルの 13:02 の判定が 0件/0件 で上書きしていた。

原因は「同じ export_day を、中身の違う2つのDBで回している」こと:

  - クラウド … その日ぶんの使い捨てSQLite。出走表も予測も揃っている
  - ローカル … 履歴DB。予測は入っていない（2026-08-23 に予測をクラウドの
                仕事にしたため）。判定を先に回した日は出走表もまだ入っていない

同じ事故は bets JSON でも 2026-08-17 に起きていて、そちらには既に
「0件なら書き換えない」という手当てがある。races 側には無かった。

⚠️ 最初に書いた修正は `id` で突き合わせていて、黙って0件しか引き継がなかった。
使い捨てDBは採番が別体系（実測: クラウド 73〜 / 履歴DB 36936〜、重なりゼロ）。
ログには「168件」と出て exit 0 だったので、出力を見るだけでは気づけない。
**キーが (場, レース番号) であること自体をテストで固定する。**
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.export import _keep_existing_race_details as keep  # noqa: E402

DATA = ROOT / "docs" / "data"


def _write(tmp_path: Path, data) -> Path:
    p = tmp_path / "races.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def _race(rid, stadium="びわこ", race_no=1, entries=None, predictions=None, **kw):
    return {"id": rid, "stadium": stadium, "race_no": race_no,
            "entries": entries or [], "predictions": predictions or [], **kw}


FULL = _race(73, entries=[{"boat_no": 1, "racer_name": "選手A"}],
             predictions=[{"boat_no": 1, "win_prob": 0.5}])


# ── 引き継ぎの基本 ──────────────────────────────

def test_db側が空なら既存を引き継ぐ(tmp_path):
    p = _write(tmp_path, [FULL])
    got = keep(p, [_race(36936)])
    assert got[0]["entries"] == FULL["entries"]
    assert got[0]["predictions"] == FULL["predictions"]


def test_db側に中身があれば上書きしない(tmp_path):
    p = _write(tmp_path, [FULL])
    fresh = _race(36936, entries=[{"boat_no": 2}], predictions=[{"boat_no": 2}])
    got = keep(p, [fresh])
    assert got[0]["entries"] == [{"boat_no": 2}]
    assert got[0]["predictions"] == [{"boat_no": 2}]


def test_着順は新しいものが残る(tmp_path):
    """判定は着順を書き足すために走る。引き継ぎがそれを潰してはいけない。"""
    p = _write(tmp_path, [dict(FULL, result_order=None)])
    got = keep(p, [_race(36936, result_order=[3, 1, 2])])
    assert got[0]["result_order"] == [3, 1, 2]
    assert got[0]["entries"]        # 出走表は引き継げている


def test_片方だけ欠けていても埋まる(tmp_path):
    p = _write(tmp_path, [FULL])
    fresh = _race(36936, entries=[{"boat_no": 9}])   # 予測だけ無い
    got = keep(p, [fresh])
    assert got[0]["entries"] == [{"boat_no": 9}]     # DB側を尊重
    assert got[0]["predictions"] == FULL["predictions"]


# ── 突き合わせキー（ここを間違えて1度失敗している） ──────────

def test_採番が違っても引き継げる(tmp_path):
    """⚠️ 本命。id で突き合わせると 0件になり、しかも黙って通る。"""
    p = _write(tmp_path, [FULL])                      # クラウド採番 73
    got = keep(p, [_race(36936)])                     # 履歴DB採番 36936
    assert got[0]["predictions"], "id で突き合わせている（採番が別体系だと必ず失敗する）"


def test_場が違えば引き継がない(tmp_path):
    p = _write(tmp_path, [FULL])
    got = keep(p, [_race(36936, stadium="戸田")])
    assert got[0]["entries"] == []


def test_レース番号が違えば引き継がない(tmp_path):
    p = _write(tmp_path, [FULL])
    got = keep(p, [_race(36936, race_no=12)])
    assert got[0]["entries"] == []


# ── 壊れた入力で落ちない ────────────────────────────

def test_ファイルが無くても落ちない(tmp_path):
    got = keep(tmp_path / "ない.json", [_race(1)])
    assert got[0]["entries"] == []


def test_壊れたjsonでも落ちない(tmp_path):
    p = tmp_path / "races.json"
    p.write_text("{壊れている", encoding="utf-8")
    assert keep(p, [_race(1)])[0]["entries"] == []


def test_listでなくても落ちない(tmp_path):
    p = _write(tmp_path, {"races": []})
    assert keep(p, [_race(1)])[0]["entries"] == []


# ── 項目を列挙して守る方式では足りなかった ───────────────
#
# 2026-08-26 の修正は entries / predictions しか守っておらず、08-29 に
# closing_time と grade が同じ経路で消えた（156レース全部）。ローカルが結果だけ
# collect して出走表を collect しなかった日は DB に締切時刻が入らず、export が
# 忠実に null で書いてクラウドの値を潰す。締切時刻は refresh_odds が
# 「買い目を確定させるか」を判断する要で、消えると永久に確定しない。

def test_締切時刻も引き継ぐ(tmp_path):
    """⚠️ 本命。08-29 に実際に消えた項目。"""
    p = _write(tmp_path, [_race(73, closing_time="10:32", grade="一般")])
    got = keep(p, [_race(36936, closing_time=None, grade=None)])
    assert got[0]["closing_time"] == "10:32"
    assert got[0]["grade"] == "一般"


def test_知らない項目も引き継ぐ(tmp_path):
    """列挙式だと、項目が増えるたび同じ事故が起きる。"""
    p = _write(tmp_path, [_race(73, なにか新しい項目="値")])
    got = keep(p, [_race(36936)])
    assert got[0]["なにか新しい項目"] == "値"


def test_dbから必ず来る項目は引き継がない(tmp_path):
    """id や場を既存から拾うと、別のレースの値が混ざりうる。"""
    p = _write(tmp_path, [_race(73, stadium="びわこ", race_no=1)])
    fresh = _race(36936, stadium="びわこ", race_no=1)
    got = keep(p, [fresh])
    assert got[0]["id"] == 36936, "id を既存から拾ってしまっている"
    assert got[0]["stadium"] == "びわこ"


def test_db側に値があれば上書きしない_締切(tmp_path):
    p = _write(tmp_path, [_race(73, closing_time="10:32")])
    got = keep(p, [_race(36936, closing_time="11:00")])
    assert got[0]["closing_time"] == "11:00"


@pytest.mark.parametrize("races_path", sorted(DATA.glob("races_2026-*.json")),
                         ids=lambda p: p.stem[6:])
def test_全レースに締切時刻がある(races_path):
    """これが欠けると買い目が確定しない。08-29 は156レース全部が空だった。

    対象は運用開始日（config の operation.live_since）以降。それ以前は
    締切時刻を取る前のデータで、全日ぶん空なのが正常
    （実測: 05-19〜08-10 の44日が空、08-11 以降は揃っている）。
    """
    from src.export import live_since
    day = races_path.stem[6:]
    if day < live_since():
        pytest.skip("運用開始前")
    races = json.loads(races_path.read_text(encoding="utf-8"))
    if not races:
        pytest.skip("レースなし")
    missing = [r for r in races if not r.get("closing_time")]
    assert not missing, f"{len(missing)}/{len(races)}レースに締切時刻が無い"


# ── probs も減らして上書きしない ─────────────────────
#
# probs はクラウドの使い捨てDBで作られ、その日の全レースぶんの予測が入る。
# 履歴DBには予測が無いので、ローカルで export_probs を走らせると買い目のある
# レースだけに縮む。2026-08-29 に手で実行して 144→36 レースに潰した。
# probs が欠けると refresh_odds がそのレースの買い目を一切作れない。

def test_probsは減るときに書き換えない(tmp_path, monkeypatch):
    import src.export as E
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    p = tmp_path / "probs_2020-01-01.json"
    prev = {"date": "2020-01-01",
            "races": [{"race_id": 1, "combinations": []},
                      {"race_id": 2, "combinations": []}]}
    p.write_text(json.dumps(prev), encoding="utf-8")

    # DB にこの日のデータは無いので、書き出そうとすると 0 レースになる
    E.export_probs(__import__("datetime").date(2020, 1, 1))

    after = json.loads(p.read_text(encoding="utf-8"))
    assert len(after["races"]) == 2, "0レースで上書きしてしまった"


# ── 実データ: 画面が実際に引けるか ──────────────────────



def _resolve(races_by_id, races_by_key, bet):
    """docs/js/app.js の openRaceModal と同じ引き方（id → 場とレース番号）。"""
    r = races_by_id.get(bet["race_id"])
    if r is None:
        r = races_by_key.get((bet.get("stadium_name"), bet.get("race_no")))
    return r


@pytest.mark.parametrize("bets_path", sorted(DATA.glob("bets_*.json")),
                         ids=lambda p: p.stem[5:])
def test_買い目からレースを引ける(bets_path):
    """買い目をタップして「レースが見つからない」が起きないこと。"""
    races_path = DATA / f"races_{bets_path.stem[5:]}.json"
    bets = json.loads(bets_path.read_text(encoding="utf-8"))
    if not bets:
        pytest.skip("買い目なし")
    assert races_path.exists(), f"{races_path.name} が無い"
    races = json.loads(races_path.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in races}
    by_key = {(r.get("stadium"), r.get("race_no")): r for r in races}
    missing = [b for b in bets if _resolve(by_id, by_key, b) is None]
    assert not missing, f"{len(missing)}件がレースを引けない（例: {missing[0]}）"


@pytest.mark.parametrize("bets_path", sorted(DATA.glob("bets_2026-08-*.json")),
                         ids=lambda p: p.stem[5:])
def test_買い目のレースに選手と確率がある(bets_path):
    """タップした先が空にならないこと。

    8月ぶんに限る。それ以前は取りこぼした個別レースが残っており、
    本件（書き出しによる全消し）とは別の話。
    """
    races_path = DATA / f"races_{bets_path.stem[5:]}.json"
    bets = json.loads(bets_path.read_text(encoding="utf-8"))
    if not bets or not races_path.exists():
        pytest.skip("買い目なし")
    races = json.loads(races_path.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in races}
    by_key = {(r.get("stadium"), r.get("race_no")): r for r in races}
    empty = [b for b in bets
             if (lambda r: r is not None and not (r.get("entries") and r.get("predictions")))
             (_resolve(by_id, by_key, b))]
    assert not empty, f"{len(empty)}件が出走表か予測が空（例: {empty[0]['stadium_name']} {empty[0]['race_no']}R）"
