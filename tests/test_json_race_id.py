"""JSON の race_id が、誰が書いても同じ番号になるかを見る。

なぜこれが要るか
----------------
JSON は2人が書く。クラウド(predict_cloud)はその日ぶんの使い捨てSQLite、
ローカルは5月からの履歴DB。どちらも `Race.id`（DBの自動採番）をそのまま
出していたため、**同じレースに2つの番号**がついていた。

    2026-08-26 実測  クラウド 1〜168 / 履歴DB 36864〜37031   重なりゼロ

JSON にはどちらの体系かが書いていないので、2つのファイルを同時に読む処理が
黙って空振りする。実害4件（08-23 別日への挿入116件 / 08-26 タップしても
中身が出ない / 08-26,27 昼から買い目が止まる / 08-24〜28 古いJSで候補が並ぶ）。

消費側を1つずつ直しても突き合わせは他にもある。**書き手が番号を揃える**のが
本筋で、`src/export.py` の `json_race_id()` がそれ。

    id = YYYYMMDD * 10000 + 場コード * 100 + レース番号
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.export import json_race_id  # noqa: E402

DATA = ROOT / "docs" / "data"
JS_MAX_SAFE_INT = 9007199254740991


# ── 計算値そのもの ──────────────────────────────

def test_書式():
    assert json_race_id(date(2026, 8, 29), "01", 1) == 202608290101
    assert json_race_id(date(2026, 8, 29), "24", 12) == 202608292412


def test_日付は文字列でも同じ():
    """export_day は date、export_probs は引数の型が揃わないことがある。"""
    assert json_race_id("2026-08-29", "01", 1) == json_race_id(date(2026, 8, 29), "01", 1)


def test_誰が書いても同じ():
    """クラウドと履歴DBで採番が違っても、この値は入力だけで決まる。"""
    a = json_race_id(date(2026, 8, 26), "12", 7)
    b = json_race_id("2026-08-26", "12", 7)
    assert a == b == 202608261207


def test_1日ぶんが重複しない():
    ids = {json_race_id(date(2026, 8, 29), f"{s:02d}", r)
           for s in range(1, 25) for r in range(1, 13)}
    assert len(ids) == 24 * 12


def test_別の日と衝突しない():
    d1 = {json_race_id(date(2026, 8, 29), f"{s:02d}", r)
          for s in range(1, 25) for r in range(1, 13)}
    d2 = {json_race_id(date(2026, 8, 30), f"{s:02d}", r)
          for s in range(1, 25) for r in range(1, 13)}
    assert not (d1 & d2)


def test_jsの安全整数に収まる():
    """画面側は JS。2^53 を超えると比較が壊れる。"""
    assert json_race_id(date(2099, 12, 31), "24", 12) < JS_MAX_SAFE_INT


def test_壊れた入力ではNoneを返す():
    """呼び出し側は `or r.id` で従来の採番に落とす。例外で export を止めない。"""
    assert json_race_id(None, "01", 1) is None
    assert json_race_id(date(2026, 8, 29), None, 1) is None
    assert json_race_id(date(2026, 8, 29), "01", None) is None
    assert json_race_id("そんな日付ない", "01", 1) is None


# ── 書き手が計算値を使っているか（ソースを直接見る）────────
#
# ⚠️ データだけ見るテストでは足りない。DB採番に戻すと「新しい体系の日」が
# 単に無くなり、テストは**落ちずにスキップ**してしまう（2026-08-29 に確認）。
# 書き手そのものを固定する。

EXPORT_SRC = (ROOT / "src" / "export.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("field,why", [
    ('"id": json_race_id(', "races JSON のレース番号"),
    ('"race_id": json_race_id(', "bets JSON のレース番号"),
    ('entry["race_id"] = json_race_id(', "probs JSON のレース番号"),
])
def test_書き手が計算値を使っている(field, why):
    assert field in EXPORT_SRC, (
        f"{why} が json_race_id を通っていない。DB採番を外に出すと"
        f"クラウドと履歴DBで番号が食い違い、突き合わせが黙って空振りする")


def test_dbの採番を直接出していない():
    """`"id": r.id` のような書き方が復活していないこと。"""
    import re
    for pat in (r'"id":\s*r\.id\b', r'"race_id":\s*b\.race_id\b'):
        assert not re.search(pat, EXPORT_SRC), \
            f"DBの採番をそのまま JSON に出している: {pat}"


# ── 実データ ────────────────────────────────────

def _days():
    return sorted(p.stem[5:] for p in DATA.glob("bets_*.json"))


def _code_of(stadium_name: str) -> str:
    st = json.loads((DATA / "stadiums.json").read_text(encoding="utf-8"))
    return {s["name"]: s["code"] for s in st}[stadium_name]


@pytest.mark.parametrize("day", _days())
def test_買い目のレースがracesに居る(day):
    """画面の突き合わせ（買い目→レース）が成立すること。

    古い日は DB 採番のままだが、その日の3ファイルが同じ体系なら成立する。
    """
    rp = DATA / f"races_{day}.json"
    bets = json.loads((DATA / f"bets_{day}.json").read_text(encoding="utf-8"))
    if not bets or not rp.exists():
        pytest.skip("買い目なし")
    rid = {r["id"] for r in json.loads(rp.read_text(encoding="utf-8"))}
    missing = {b["race_id"] for b in bets} - rid
    assert not missing, f"{len(missing)}レースが races に居ない（採番が食い違っている）"


@pytest.mark.parametrize("day", _days())
def test_計算値で書かれた日は場とレース番号から復元できる(day):
    """新しい体系の日は、id が (日付, 場, レース番号) から再現できること。

    ⚠️ 「3ファイルが同じ体系か」は不変条件にできない。probs はクラウドだけが
    書き、races/bets はローカルの判定が上書きするので、**判定が走った日は
    必ず混在する**（2026-08-28 が実際にそう。probs 1〜 / races 37344〜）。
    移行期の話ではなく通常の状態なので、噛み合わせは
    `index_probs_by_race` が担う（tests/test_probs_race_id.py で確認）。
    ここでは「計算値が計算値であること」だけを見る。
    """
    rp = DATA / f"races_{day}.json"
    if not rp.exists():
        pytest.skip("ファイルなし")
    races = json.loads(rp.read_text(encoding="utf-8"))
    # 計算値は YYYYMMDD*10000 なので 2e11 を超える。旧採番（数万台）と区別できる。
    new = [r for r in races if isinstance(r.get("id"), int) and r["id"] > 200_000_000_000]
    if not new:
        pytest.skip("旧採番の日")
    for r in new:
        assert r["id"] == json_race_id(day, _code_of(r["stadium"]), r["race_no"]), \
            f"{r['stadium']} {r['race_no']}R の id が計算値と合わない"
