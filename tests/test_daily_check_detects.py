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

from datetime import timedelta, timezone  # noqa: E402

from daily_check import (  # noqa: E402
    _final_picks_lost, bets_revisions, json_integrity_checks,
    lost_final_picks, night_runs, unbuyable_money_bets)

JST9 = timezone(timedelta(hours=9))


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


# ── 確定した買い目が消えていないか ────────────────────
# 日中に消えてよいのは「まだ確定していない買い目」だけ。確定したものが
# 消えるのは記録の破壊で、後から損益が書き換わる。

def _bet(stadium, no, combo, final=True, race_id=1):
    return {"race_id": race_id, "stadium_name": stadium, "race_no": no,
            "bet_type": "nirenfuku", "combination": combo,
            "is_final_pick": final}


def test_確定した買い目が消えたら検知する():
    revs = [
        [_bet("桐生", 1, "1-2"), _bet("桐生", 2, "1-3")],
        [_bet("桐生", 1, "1-2")],                       # R2 の確定が消えた
    ]
    lost = lost_final_picks(revs)
    assert len(lost) == 1, f"見逃した: {lost}"
    assert ("桐生", 2, "nirenfuku", "1-3") in lost


def test_未確定が入れ替わっても警報を出さない():
    """オッズが動けば EV が変わるので、未確定は入れ替わって当然。"""
    revs = [
        [_bet("桐生", 1, "1-2", final=False)],
        [_bet("桐生", 1, "1-4", final=False)],
    ]
    assert lost_final_picks(revs) == set()


def test_採番が入れ替わっても誤検知しない():
    """⚠️ この誤検知を 2026-08-31 に実際に出した（08-28 で32本）。

    クラウドとローカルで race_id の体系が違う。同じ買い目が別 id に
    なるだけで「消えた」と数えてはいけない。
    """
    revs = [
        [_bet("桐生", 1, "1-2", race_id=73)],           # クラウド採番
        [_bet("桐生", 1, "1-2", race_id=36936)],        # 履歴DB採番
    ]
    assert lost_final_picks(revs) == set(), "race_id で見ている（採番違いに弱い）"


def test_実データでは確定が消えていない():
    """08-26〜08-31 の実際の履歴。ここが NG なら本当に記録が壊れている。"""
    src = ROOT / "docs" / "data"
    days = sorted(p.name[5:15] for p in src.glob("bets_2026-*.json"))[-5:]
    if not days:
        pytest.skip("bets JSON が無い")
    checked = 0
    for day in days:
        got = _final_picks_lost(day, src)
        if got is None:
            continue
        n_lost, n_rev = got
        assert n_lost == 0, f"{day}: 確定買い目が {n_lost}本 消えている（{n_rev}版）"
        checked += 1
    if not checked:
        pytest.skip("git 履歴なし")


# ── 締切より後に生えた買い目 ────────────────────
# 2026-08-31: 朝のクラウド実行が最速レースの締切に間に合わず、
# 芦屋R2(締切08:58) の買い目が 09:43 に 500円 付きで初めて現れた。

def _rev(hhmm, bets):
    from datetime import datetime as dt
    return (dt.strptime(f"2026-08-31 {hhmm}", "%Y-%m-%d %H:%M")
            .replace(tzinfo=JST9), bets)


def _mb(rn, ct, amount=500):
    return {"stadium_name": "芦屋", "race_no": rn, "bet_type": "nirenfuku",
            "combination": "1-2", "closing_time": ct,
            "recommended_amount": amount}


def test_締切後に生えた買い目を検知する():
    revs = [_rev("09:43", [_mb(2, "08:58"), _mb(5, "11:30")])]
    bad = unbuyable_money_bets(revs, "2026-08-31")
    assert len(bad) == 1, f"R2 だけが該当のはず: {bad}"
    assert bad[0][1] == 2 and bad[0][4] == 45, bad


def test_締切前に出ていれば警報を出さない():
    revs = [_rev("08:00", [_mb(2, "08:58")])]
    assert unbuyable_money_bets(revs, "2026-08-31") == []


def test_更新が止まっただけの買い目は警報を出さない():
    """⚠️ ここを is_final_pick で判定すると誤検知する。

    2026-08-26 は 13:16 に買い目生成が止まり、午後のレース8本が確定
    しないまま残った。**朝から画面に出ていて買えた**買い目なので、
    除外してはいけない。「初めて現れた時刻」で見ればここは通る。
    """
    revs = [_rev("09:00", [_mb(7, "14:26")]),      # 朝に出ている
            _rev("13:16", [_mb(7, "14:26")])]      # 以後 更新が止まった
    assert unbuyable_money_bets(revs, "2026-08-31") == []


def test_賭け金0なら締切後に出ても警報を出さない():
    """記録のみの買い目は損益に入らないので害が無い。"""
    revs = [_rev("09:43", [_mb(2, "08:58", amount=0)])]
    assert unbuyable_money_bets(revs, "2026-08-31") == []


def test_実データで8月31日の2本が挙がる():
    src = ROOT / "docs" / "data"
    revs = bets_revisions("2026-08-31", src)
    if not revs:
        pytest.skip("git 履歴なし")
    bad = unbuyable_money_bets(revs, "2026-08-31")
    assert len(bad) == 2, f"芦屋R2/R3 の2本が挙がるはず: {bad}"
    assert {b[1] for b in bad} == {2, 3}, bad


# ── 賭式が欠けていないか ────────────────────

def test_賭式が欠けたら検知する(tmp_path):
    """6賭式のうち片方の経路だけ古いと黙って賭式が減る。件数では出ない。"""
    day = "2026-08-31"
    only_one = [{"race_id": 1, "stadium_name": "桐生", "race_no": 1,
                 "bet_type": "nirenfuku", "combination": "1-2"} for _ in range(135)]
    checks, _ = json_integrity_checks(day, _stage(tmp_path, day, bets=only_one))
    ok, detail = _result(checks, "賭式の欠け")
    assert not ok, f"見逃した: {detail}"
    assert "tansho" in detail and "sanrentan" in detail, detail


def test_6賭式そろっていれば警報を出さない(tmp_path):
    day = "2026-08-31"
    types = ["tansho", "fukusho", "kakurenfuku",
             "nirenfuku", "sanrenfuku", "sanrentan"]
    bets = [{"race_id": 1, "stadium_name": "桐生", "race_no": 1,
             "bet_type": t, "combination": "1-2"} for t in types]
    checks, _ = json_integrity_checks(day, _stage(tmp_path, day, bets=bets))
    ok, detail = _result(checks, "賭式の欠け")
    assert ok, detail


# ── 夜間処理が完走したか ────────────────────
# 2026-08-28 22:30 の実行は DB 初期化まで書いて消滅し（PC が落ちた）、
# 8/29 は起動もせず、8/30 09:00 の取りこぼし起動でようやく 8/29 分が
# 処理された。その3日間、警告は一度も出ていない。

def test_途中で死んだ実行を検知する():
    lines = [
        "[2026/08/27 22:30:24.71] JUDGE start ",
        "[2026/08/27 22:31:45.99] JUDGE done ",
        "[2026/08/28 22:30:17.30] JUDGE start ",     # done が来ないまま
        "[2026/08/30  9:00:56.50] JUDGE start ",
        "[2026/08/30  9:15:53.64] JUDGE done ",
    ]
    runs = night_runs(lines)
    assert [ok for _, ok in runs] == [True, False, True], runs


def test_失敗して終わった実行は完走扱い():
    """自分で failed と書けたなら、少なくとも最後まで到達している。
    黙って消えるのとは区別する（前者は exit code で分かる）。"""
    lines = [
        "[2026/08/27 22:30:24.71] JUDGE start ",
        "[2026/08/27 22:31:45.99] JUDGE failed ",
    ]
    assert [ok for _, ok in night_runs(lines)] == [True]


def test_実ログで8月28日の未完走が見える():
    log = ROOT / "logs" / "task_judge.log"
    if not log.exists():
        pytest.skip("task_judge.log が無い")
    runs = night_runs(log.read_text(encoding="utf-8", errors="replace").splitlines())
    dead = [w for w, ok in runs if not ok]
    assert any("08/28" in w for w in dead), \
        f"08-28 の未完走を見落としている（未完走と判定したのは {dead}）"


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
