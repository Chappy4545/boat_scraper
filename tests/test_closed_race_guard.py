"""締切を過ぎたレースに賭け金を付けていないかを見る。

2026-08-31 発見
---------------
朝のクラウド実行が、その日いちばん早いレースの締切に間に合っていない。
初回の書き出しが 09:43 頃、最速のレースは 08:48〜09:26 に締切。
git の版を遡って実測:

    芦屋R2  締切 08:58 → 初めて JSON に現れたのは 09:43（46分後）
    芦屋R3  締切 09:24 → 初めて JSON に現れたのは 09:43（20分後）
    どちらも 500円 が付いたまま損益に入っていた

08-20 以降12日で 24本（金額つきの7%）。回収率を 1.3pt 下振れさせ、
日単位では 118% と 131% ほど違う。

refresh_odds には 2026-08-12 の事故のあと同じ守りが入っていたが、
朝の書き出し経路には無かった。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main import _closed_race_ids  # noqa: E402

JST = timezone(timedelta(hours=9))
D = date(2026, 8, 31)


def at(h, m):
    return datetime(2026, 8, 31, h, m, tzinfo=JST)


CLOSING = {1: "08:58", 2: "09:24", 3: "11:30", 4: "20:40"}


def test_締切を過ぎたレースを締切済みと判定する():
    """09:43 の書き出し時点。実際に起きた状況。"""
    got = _closed_race_ids(D, CLOSING, now=at(9, 43))
    assert got == {1, 2}, f"08:58 と 09:24 が締切済みのはず: {got}"


def test_朝一番なら1つも締切済みでない():
    assert _closed_race_ids(D, CLOSING, now=at(8, 0)) == set()


def test_締切ちょうどは締切済み扱い():
    """締切時刻に達したら買えない。境界は安全側に倒す。"""
    assert 1 in _closed_race_ids(D, CLOSING, now=at(8, 58))
    assert 1 not in _closed_race_ids(D, CLOSING, now=at(8, 57))


def test_締切時刻が無いレースは締切済みにしない():
    """⚠️ 分からないものを締切後と決めると、締切時刻の取得が壊れた日に
    買い目が全滅する（2026-08-29 に closing_time が156件全消しになった）。"""
    got = _closed_race_ids(D, {1: None, 2: "", 3: "こわれた"}, now=at(23, 0))
    assert got == set(), f"分からないものを締切済みにしている: {got}"


def test_過去日は全部締切済み():
    got = _closed_race_ids(date(2026, 8, 30), CLOSING, now=at(9, 43))
    assert got == set(CLOSING), "過去日のレースはもう買えない"


def test_未来日は1つも締切済みでない():
    got = _closed_race_ids(date(2026, 9, 1), CLOSING, now=at(23, 59))
    assert got == set()

# ⚠️ 単体でこの関数が正しくても、cmd_predict が使っていなければ意味がない。
# 経路そのものを通す確認は tests/test_pipeline_e2e.py の
# `test_締切を過ぎたレースに賭け金が付かない` にある。
