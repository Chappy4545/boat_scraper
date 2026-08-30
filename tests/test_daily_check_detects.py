"""点検が、実際に起きた壊れ方を検知できるかを見る。

2026-08 の1週間で4件の故障が起きたが、`daily_check.py` は毎晩
**「すべて正常」**と出し続けていた。件数と鮮度しか見ていなかったため。
壊れ方はどれも「行はあるが中身が空」「ファイル同士が噛み合わない」で、
件数では出ない。ユーザーが画面を見て気づくまで誰も知らなかった。

このテストは **git に残っている実際の壊れたデータ**を読み込ませて、
検知できることを確かめる。作り物の入力ではなく、その日そこにあったもの。

    08-26 races が entries/predictions とも 0 件（ad41dae）
    08-26 probs(クラウド採番) と races(履歴DB採番) が一致 0 件
    08-29 races の closing_time が 156件全滅（845e783）
    08-26 買い目が 31→11本（0e78ed9）

git に無い環境（浅いクローン等）では skip する。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from daily_check import json_integrity_checks  # noqa: E402


def _show(rev: str, path: str):
    r = subprocess.run(["git", "show", f"{rev}:{path}"],
                       cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except Exception:
        return None


def _stage(tmp_path: Path, day: str, races=None, bets=None, probs=None):
    for name, obj in (("races", races), ("bets", bets), ("probs", probs)):
        if obj is not None:
            (tmp_path / f"{name}_{day}.json").write_text(
                json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _result(checks, name):
    for n, ok, detail in checks:
        if n == name:
            return ok, detail
    pytest.fail(f"検査 '{name}' が出ていない（出たのは {[c[0] for c in checks]}）")


# ── 実際に壊れたデータで検知できるか ────────────────────

def test_出走表と予測が全消しを検知する(tmp_path):
    """08-26: ローカルの判定が上書きした版。タップしても何も出なかった。"""
    broken = _show("ad41dae", "docs/data/races_2026-08-26.json")
    if broken is None:
        pytest.skip("git 履歴なし")
    assert sum(1 for r in broken if r.get("entries")) == 0, "前提: これは壊れた版"
    checks, _ = json_integrity_checks("2026-08-26", _stage(tmp_path, "2026-08-26", races=broken))
    ok, detail = _result(checks, "出走表と予測")
    assert not ok, f"見逃した: {detail}"


def test_締切時刻が全消しを検知する(tmp_path):
    """08-29: 買い目が永久に確定しなくなる壊れ方。"""
    broken = _show("845e783", "docs/data/races_2026-08-29.json")
    if broken is None:
        pytest.skip("git 履歴なし")
    assert sum(1 for r in broken if r.get("closing_time")) == 0, "前提: これは壊れた版"
    checks, _ = json_integrity_checks("2026-08-29", _stage(tmp_path, "2026-08-29", races=broken))
    ok, detail = _result(checks, "締切時刻")
    assert not ok, f"見逃した: {detail}"


def test_採番が食い違っても買い目は作れる(tmp_path):
    """08-26/27 に昼から買い目生成が止まった、その実データ。

    採番の食い違い自体は（場とレース番号での橋渡しを入れたので）もう
    致命傷ではない。検査が見るのは「食い違っているか」ではなく
    **買い目を作れるか**。作れるなら OK で、橋渡しが要った事実は詳細に出す。
    """
    probs = _show("cbb5fa3", "docs/data/probs_2026-08-26.json")   # クラウド採番
    races = _show("7e83648", "docs/data/races_2026-08-26.json")   # 履歴DB採番
    if probs is None or races is None:
        pytest.skip("git 履歴なし")
    pid = {e["race_id"] for e in probs.get("races", [])}
    assert not (pid & {r["id"] for r in races}), "前提: 採番が食い違っている"
    checks, _ = json_integrity_checks(
        "2026-08-26", _stage(tmp_path, "2026-08-26", races=races, probs=probs))
    ok, detail = _result(checks, "probsとracesの対応")
    assert ok, f"橋渡しが効いていない: {detail}"
    assert "橋渡し" in detail, f"食い違いが見えていない: {detail}"


def test_引けないレースがあれば検知する(tmp_path):
    """橋渡しでも救えない場合。ここが本当に「買い目が作れない」状態。"""
    races = [{"id": 1, "stadium": "桐生", "race_no": 1,
              "closing_time": "10:32", "entries": [{}], "predictions": [{}]}]
    probs = {"date": "2026-08-26", "races": [
        {"race_id": 1, "stadium_code": "01", "race_no": 1, "combinations": []},
        {"race_id": 2, "stadium_code": "02", "race_no": 5, "combinations": []},
    ]}
    checks, _ = json_integrity_checks(
        "2026-08-26", _stage(tmp_path, "2026-08-26", races=races, probs=probs))
    ok, detail = _result(checks, "probsとracesの対応")
    assert not ok, f"見逃した: {detail}"


def test_買い目の激減を検知する(tmp_path):
    """08-26: 31本→11本。件数>0 の検査では見えなかった。"""
    before = _show("ad41dae", "docs/data/bets_2026-08-26.json")   # 42行
    after = _show("0e78ed9", "docs/data/bets_2026-08-26.json")    # 15行
    if before is None or after is None:
        pytest.skip("git 履歴なし")
    assert len(after) < len(before) * 0.8, "前提: 実際に激減している"
    checks, _ = json_integrity_checks(
        "2026-08-26", _stage(tmp_path, "2026-08-26", bets=after),
        prev_health={"date": "2026-08-26", "bets_peak": len(before)})
    ok, detail = _result(checks, "買い目の目減り")
    assert not ok, f"見逃した: {detail}"


# ── 正常なデータで誤警報を出さないか ────────────────────

def test_正常な日は警報を出さない(tmp_path):
    """08-28 はクラウドが1日通して動き、3ファイルとも噛み合っていた。"""
    day = "2026-08-28"
    src = ROOT / "docs" / "data"
    for name in ("races", "bets", "probs"):
        p = src / f"{name}_{day}.json"
        if not p.exists():
            pytest.skip(f"{p.name} なし")
        (tmp_path / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    checks, peak = json_integrity_checks(day, tmp_path)
    ng = [(n, t) for n, ok, t in checks if not ok]
    assert not ng, f"正常な日に警報が出た: {ng}"
    assert peak > 0


def test_ファイルが無い日は警報を出さない(tmp_path):
    """朝、クラウドが書く前。ここで鳴ると毎日誤警報になる。"""
    checks, peak = json_integrity_checks("2099-01-01", tmp_path)
    assert [c for c in checks if not c[1]] == []
    assert peak == 0
