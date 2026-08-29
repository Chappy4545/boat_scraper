"""probs と races で race_id の体系が違っても買い目を作れるかを見る。

2026-08-26〜27 の事故
--------------------
「日中に買い目が増えなくなる」日があった。原因は race_id の体系差:

    probs_<日>.json  … クラウド predict_cloud が使い捨てSQLite（1から採番）で書く
    races_<日>.json  … ローカルが日中に走ると履歴DBの採番で上書きする

    実測の一致数   2026-08-26  0/168
                   2026-08-27  0/156
                   2026-08-28  144/144（ローカルが日中に走らなかった日）

一致0だと `cmd_refresh_odds` は1レースも引けず、確定済み以外の買い目が消える:

    08/26 13:02 ローカルが判定 → 13:16 クラウド  買い31本 → 11本
    08/27 13:04 ローカルが判定 → 13:10 クラウド  買い23本 → 10本

⚠️ **エラーは出ない。** 対象0レースとして静かに終わる。しかもローカルが朝に
走った日は採番が揃うので再現しない。日によって出たり出なかったりする。

`_sync_bets_from_json` は 2026-08-23 の同種事故（別の日のレースに買い目が
116件挿入された）で既に場名とレース番号で引き直している。同じ手当てを
`index_probs_by_race` で入れた。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main import index_probs_by_race  # noqa: E402

DATA = ROOT / "docs" / "data"


def _probs(*entries):
    return {"date": "2026-08-27", "races": list(entries)}


def _p(race_id, code="01", race_no=1):
    return {"race_id": race_id, "stadium_code": code, "race_no": race_no,
            "combinations": []}


def _r(rid, stadium="桐生", race_no=1):
    return {"id": rid, "stadium": stadium, "race_no": race_no}


# stadiums.json の実物にある対応（code "01" は桐生）を使う。
# これが変わったらテストごと直すべきなので、ここで固定しておく。
def test_前提_stadiums_jsonにcode01がある():
    st = json.loads((DATA / "stadiums.json").read_text(encoding="utf-8"))
    assert {s["code"]: s["name"] for s in st}.get("01") == "桐生"


# ── 採番が違う場合 ────────────────────────────────

def test_採番が違っても引ける():
    """⚠️ 本命。race_id で直に突き合わせると 0 件になる。"""
    got = index_probs_by_race(_probs(_p(1)), [_r(36864)], [36864])
    assert list(got) == [36864], "場とレース番号で引き直せていない"
    assert got[36864]["race_id"] == 1   # 中身は probs のまま


def test_採番が違うとき対象外のレースは入らない():
    got = index_probs_by_race(_probs(_p(1)), [_r(36864)], [])
    assert got == {}


# ── 採番が揃っている場合（従来どおり） ──────────────────

def test_採番が同じならそのまま():
    got = index_probs_by_race(_probs(_p(7)), [_r(7)], [7])
    assert list(got) == [7]


def test_対象レースだけに絞る():
    probs = _probs(_p(1, "01", 1), _p(2, "01", 2))
    races = [_r(101, "桐生", 1), _r(102, "桐生", 2)]
    got = index_probs_by_race(probs, races, [101])
    assert list(got) == [101]


# ── 引けないときの退避 ──────────────────────────────

def test_場名が引けなければrace_idで引く():
    """stadiums.json に無いコード。採番が揃っていれば従来どおり動くこと。"""
    got = index_probs_by_race(_probs(_p(7, code="99")), [_r(7, "どこか")], [7])
    assert list(got) == [7]


def test_racesに無いレースは落とす():
    got = index_probs_by_race(_probs(_p(1, "01", 5)), [_r(101, "桐生", 1)], [101])
    assert got == {}


def test_probsが空でも落ちない():
    assert index_probs_by_race({"races": []}, [_r(1)], [1]) == {}
    assert index_probs_by_race({}, [_r(1)], [1]) == {}


# ── 実データ ────────────────────────────────────

@pytest.mark.parametrize("day,expect_remap", [
    ("2026-08-26", True),    # ローカルが日中に走った日
    ("2026-08-27", True),
    ("2026-08-28", False),   # 揃っていた日
])
def test_実データで全レース引ける(day, expect_remap):
    probs_p = DATA / f"probs_{day}.json"
    races_p = DATA / f"races_{day}.json"
    if not (probs_p.exists() and races_p.exists()):
        pytest.skip(f"{day} のJSONなし")
    probs = json.loads(probs_p.read_text(encoding="utf-8"))
    races = json.loads(races_p.read_text(encoding="utf-8"))
    upcoming = [r["id"] for r in races]

    naive = {e["race_id"] for e in probs.get("races", [])} & set(upcoming)
    got = index_probs_by_race(probs, races, upcoming)

    assert len(got) == len(races), f"{len(races)}レース中 {len(got)}レースしか引けない"
    if expect_remap:
        assert not naive, "この日は採番が揃っていない前提のテスト（前提が変わった）"
