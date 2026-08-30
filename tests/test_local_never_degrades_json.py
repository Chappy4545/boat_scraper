"""ローカルの判定が JSON を劣化させないことを見る。

なぜこのテストが要るか
--------------------
JSON を書く役割が2人いた。

    クラウド predict_cloud … その日ぶんの使い捨てSQLite。データが揃っている
    ローカル cmd_judge    … 履歴DB。**中身が欠けている**
                             予測なし / 出走表なし / 締切時刻なし / 買い目なし

どちらも `export_day`（DBの中身で JSON を作り直す）を呼んでいたため、
ローカルが走るたびに欠けている分が null で上書きされた。
2026-08 の1週間で4回:

    08-26 出走表と予測が全消し → タップしても選手も確率も出ない
    08-26/27 採番の食い違いで昼から買い目生成が停止（31本→11本 / 23本→10本）
    08-28 判定が途中で切れて1日ぶんDBに入らず
    08-29 締切時刻とグレードが全消し → 買い目が確定しない

消えた項目を1つずつ守る方式では追いつかなかった
（entries → predictions → closing_time → grade と、そのつど別の項目が消えた）。

役割を分けたのが本修正:

    クラウド … JSON を作る（export_day）
    ローカル … 結果だけ書き足す（fill_results_into_json）

このテストは **ローカル側が「増やすだけ」であること** を固定する。
実データではなく、欠けたDBをその場で組んで確かめる。
実測（08-27〜29）ではクラウドの最終版が全行に判定と着順を持っており、
ローカル由来なのは races の着順だけ。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.export as E  # noqa: E402
from src.ingestion import database as DB  # noqa: E402
from src.ingestion.models import Race, RaceResult, Stadium  # noqa: E402
from src.utils.helpers import load_config  # noqa: E402

D = date(2026, 8, 29)

# クラウドが朝に書いた JSON（データが揃っている状態）
CLOUD_RACES = [{
    "result_order": None,
    "id": 202608290101,
    "race_date": "2026-08-29",
    "stadium": "桐生",
    "race_no": 1,
    "grade": "一般",
    "race_type": "予選",
    "closing_time": "10:32",
    "is_night": False,
    "predictions": [{"boat_no": 1, "win_prob": 0.51}],
    "entries": [{"boat_no": 1, "racer_name": "選手A"}],
}]
CLOUD_BETS = [{
    "bet_id": None, "race_id": 202608290101, "stadium_name": "桐生", "race_no": 1,
    "grade": "一般", "race_type": "予選", "closing_time": "10:32", "is_night": False,
    "bet_type": "nirenfuku", "combination": "1-2", "model_prob": 0.33, "odds": 4.2,
    "expected_value": 1.39, "recommended_amount": 500,
    "is_hit": None, "actual_payout": None, "result_order": None, "is_final_pick": True,
}]


@pytest.fixture
def 欠けたDB(tmp_path, monkeypatch):
    """履歴DBの実態を再現する。

    レースと着順はあるが、**予測・出走表・締切時刻・グレード・買い目が無い**。
    2026-08-29 の実際の状態そのもの。
    """
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    DB.init_db({"database": {"url": f"sqlite:///{(tmp_path / 't.db').as_posix()}"}})
    with DB.get_session() as s:
        s.add(Stadium(id=1, code="01", name="桐生"))
        s.add(Race(id=99001, race_date=D, stadium_id=1, race_no=1))  # 締切もグレードも無い
    with DB.get_session() as s:
        for order, boat in enumerate([3, 1, 2, 4, 5, 6], start=1):
            s.add(RaceResult(race_id=99001, arrival_order=order, boat_no=boat))
    (tmp_path / f"races_{D}.json").write_text(
        json.dumps(CLOUD_RACES, ensure_ascii=False), encoding="utf-8")
    (tmp_path / f"bets_{D}.json").write_text(
        json.dumps(CLOUD_BETS, ensure_ascii=False), encoding="utf-8")
    yield tmp_path
    DB.init_db(load_config())      # 本物のDBへ戻す


def _load(p: Path, name):
    return json.loads((p / f"{name}_{D}.json").read_text(encoding="utf-8"))


# ── 本命: 増えるだけで、減らない ──────────────────────

def test_欠けたDBでも項目が消えない(欠けたDB):
    """⚠️ 08-29 に実際に消えた項目を、DBが空のまま守れること。"""
    E.fill_results_into_json(D)
    r = _load(欠けたDB, "races")[0]
    assert r["closing_time"] == "10:32", "締切時刻が消えた（買い目が確定しなくなる）"
    assert r["grade"] == "一般"
    assert r["entries"], "出走表が消えた"
    assert r["predictions"], "予測が消えた"
    assert r["id"] == 202608290101, "採番が書き換わった"


def test_買い目が消えない(欠けたDB):
    """DBに買い目が1件も無くても、JSON の行は残ること。"""
    E.fill_results_into_json(D)
    assert len(_load(欠けたDB, "bets")) == 1


def test_着順は書き足される(欠けたDB):
    """ローカルにしかできない仕事。これができないと画面に着順が出ない。"""
    E.fill_results_into_json(D)
    assert _load(欠けたDB, "races")[0]["result_order"] == [3, 1, 2, 4, 5, 6]
    assert _load(欠けたDB, "bets")[0]["result_order"] == [3, 1, 2, 4, 5, 6]


def test_変化は着順の追加だけ(欠けたDB):
    """**増えるだけ**を厳密に確認する。他の項目が1つでも動いたら落ちる。"""
    before = {"races": _load(欠けたDB, "races"), "bets": _load(欠けたDB, "bets")}
    E.fill_results_into_json(D)
    after = {"races": _load(欠けたDB, "races"), "bets": _load(欠けたDB, "bets")}
    for kind in ("races", "bets"):
        assert len(after[kind]) == len(before[kind]), f"{kind} の行数が変わった"
        for a, b in zip(before[kind], after[kind]):
            changed = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
            assert changed <= {"result_order"}, \
                f"{kind} で着順以外が変わった: {changed}"


def test_既にある判定を上書きしない(欠けたDB):
    """クラウドの judge_live が入れた結果を、ローカルが塗り替えないこと。"""
    p = 欠けたDB / f"bets_{D}.json"
    bets = json.loads(p.read_text(encoding="utf-8"))
    bets[0]["is_hit"], bets[0]["actual_payout"] = True, 1230
    p.write_text(json.dumps(bets, ensure_ascii=False), encoding="utf-8")
    E.fill_results_into_json(D)
    got = _load(欠けたDB, "bets")[0]
    assert got["is_hit"] is True and got["actual_payout"] == 1230


def test_二度走らせても変わらない(欠けたDB):
    """判定はキャッチアップで何度も走る。"""
    E.fill_results_into_json(D)
    once = _load(欠けたDB, "races"), _load(欠けたDB, "bets")
    E.fill_results_into_json(D)
    assert (_load(欠けたDB, "races"), _load(欠けたDB, "bets")) == once


def test_JSONが無い日は作りにいく(欠けたDB):
    """クラウドが一度も動かなかった日は、無いよりましなので作る。"""
    (欠けたDB / f"races_{D}.json").unlink()
    (欠けたDB / f"bets_{D}.json").unlink()
    E.fill_results_into_json(D)
    assert (欠けたDB / f"races_{D}.json").exists()


# ── 呼び出し側が戻っていないか（ソースを直接見る）──────────

def test_judgeはexport_dayを呼ばない():
    """⚠️ 4件の故障の発生源。ここが戻ると全部再発する。

    コメントには「export_day を呼ぶな」と書いてあるので、
    **コメントを除いてから**実際の呼び出しを見る。
    """
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("def cmd_judge(")
    body = src[i:src.index("\ndef ", i + 10)]
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    assert "fill_results_into_json" in code, "結果の書き足しを呼んでいない"
    assert "export_day" not in code, \
        "cmd_judge が export_day を呼んでいる（履歴DBの欠損で JSON を潰す）"
