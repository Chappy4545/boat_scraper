"""結果収集が「空を返したまま成功扱いになる」のを防げているかを見る。

2026-08-22 の朝、144 レース中 137 レースが空で返り、例外も警告も出ないまま
「結果収集完了」と記録された。判定は 38 本中 1 本しかできていない。
手で1回叩き直したら 849 件取れたので、その場でやり直せば戻せる失敗だった。

ここで確かめるのは2つ:
  1. 空で返ったレースを検知して取り直すか
  2. 取り直しでも空なら、黙って成功にせず記録が残るか
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.scraping.official import BoatRaceScraper


def _result_df(vc: str, rn: int) -> pd.DataFrame:
    return pd.DataFrame([
        {"stadium_code": vc, "race_date": date(2026, 8, 21), "race_no": rn,
         "arrival_order": i, "boat_no": i, "racer_no": 1000 + i, "race_time": 110.0}
        for i in range(1, 7)
    ])


def _payout_df(vc: str, rn: int) -> pd.DataFrame:
    return pd.DataFrame([
        {"stadium_code": vc, "race_date": date(2026, 8, 21), "race_no": rn,
         "bet_type": "nirenfuku", "combination": "1-2", "payout": 300}
    ])


class _Stub(BoatRaceScraper):
    """collect_day_results だけを動かすための最小の差し替え。

    fail_first に入れたレースは1回目だけ空を返す（本番で起きた症状の再現）。
    always_empty は何度呼んでも空を返す。
    """

    def __init__(self, finished, fail_first=(), always_empty=()):
        self._finished = list(finished)
        self._fail_first = set(fail_first)
        self._always_empty = set(always_empty)
        self.calls: list[tuple[str, int]] = []
        self._config = {}
        self.base_url = "https://example.invalid"

    def _fetch_raw(self, url, params=None):      # noqa: D401
        return "<html></html>"

    def parse_pay_summary(self, html):
        return self._finished

    def get_race_result_and_payouts(self, venue_code, race_date, race_no):
        key = (venue_code, race_no)
        self.calls.append(key)
        if key in self._always_empty:
            return pd.DataFrame(), pd.DataFrame()
        if key in self._fail_first:
            self._fail_first.discard(key)        # 2回目からは返す
            return pd.DataFrame(), pd.DataFrame()
        return _result_df(venue_code, race_no), _payout_df(venue_code, race_no)


def test_空で返ったレースを取り直して回収する():
    finished = [("02", n) for n in range(1, 13)]
    # 本番の症状: ほとんどが1回目だけ空
    stub = _Stub(finished, fail_first=finished[2:])

    out = stub.collect_day_results(date(2026, 8, 21), max_workers=1)

    assert len(out["race_result"]) == 12 * 6, "全レース回収できていない"
    # 空だった10レースは2回呼ばれている
    assert len(stub.calls) == 12 + 10


def test_取り直しでも空なら記録が残る():
    # このプロジェクトは loguru を使っており、標準 logging の caplog には
    # 流れてこない。専用のシンクを挿して受ける。
    from loguru import logger as _lg

    lines: list[str] = []
    sink_id = _lg.add(lines.append, level="WARNING")
    try:
        finished = [("02", n) for n in range(1, 13)]
        stub = _Stub(finished, always_empty=finished[:5])
        out = stub.collect_day_results(date(2026, 8, 21), max_workers=1)
    finally:
        _lg.remove(sink_id)

    # 取れた分は返る
    assert len(out["race_result"]) == 7 * 6
    # 黙って成功にしない
    text = "".join(lines)
    assert "取り直します" in text
    assert "取れなかった" in text


def test_全部取れたときは取り直さない():
    finished = [("02", n) for n in range(1, 13)]
    stub = _Stub(finished)

    out = stub.collect_day_results(date(2026, 8, 21), max_workers=1)

    assert len(out["race_result"]) == 12 * 6
    assert len(stub.calls) == 12, "余計な取り直しが走っている"
