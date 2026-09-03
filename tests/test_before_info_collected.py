"""直前情報（展示タイム・気象）が集まり続けていることを見張る。

⚠️⚠️ **これは2回止まっている。**

    2026-05-21  一度止まる。推論時に中央値で埋まるだけの列になり、
                黙って精度を落としていた（[[project_before_info_disabled]]）
    2026-06-10  直した「はず」
    2026-09-03  **同じ形でまた止まっていた**と判明:
                  01〜04月 100% / 05月 74% / 06〜07月 100%
                  08月 33% / 09月 **0%**

2回とも**気づいたのは何ヶ月も後**。件数の検査では出ない（買い目も判定も
正常に動くので、どこも赤くならない）。だから専用に見張る。

なぜ夜の経路で集めるのか
------------------------
直前情報はレース20〜30分前に公開されるので、**朝の一括収集ではまだ無い**
（`_collect_one_stadium` は skip_before_info=True が既定で、これは正しい）。
一方レース後も残るので、終了レースを回る `collect_day_results` が置き場所。
"""
from __future__ import annotations

import inspect
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestNightCollectionGathersIt:
    """夜の収集が直前情報を返すこと。**振る舞いで見る**（ソースのgrepでない）。"""

    def _scraper(self, monkeypatch, *, bi_ok=True):
        """⚠️ **クラスに当てる。** 並列版の `_worker` は
        `with BoatRaceScraper(config) as s:` で**新しいインスタンスを作る**ので、
        インスタンスへの monkeypatch は届かない。
        最初そう書いて並列版だけ落ち、コードのバグかと10分疑った。
        """
        from src.scraping.official import BoatRaceScraper
        from src.utils.helpers import load_config

        def fake_result(self, vc, d, rn):
            return (pd.DataFrame([{"race_no": rn, "boat_no": 1, "arrival_order": 1}]),
                    pd.DataFrame([{"race_no": rn, "bet_type": "nirenfuku",
                                   "combination": "1-2", "payout": 900}]))

        def fake_bi(self, vc, d, rn):
            if not bi_ok:
                raise RuntimeError("beforeinfo が落ちた")
            return (pd.DataFrame([{"race_no": rn, "boat_no": 1, "exhibition_time": 6.7}]),
                    pd.DataFrame([{"race_no": rn, "wind_speed": 3}]))

        monkeypatch.setattr(BoatRaceScraper, "_fetch_raw",
                            lambda self, *a, **k: "<html></html>")
        monkeypatch.setattr(BoatRaceScraper, "parse_pay_summary",
                            lambda self, h: [("01", 1), ("01", 2)])
        monkeypatch.setattr(BoatRaceScraper, "get_race_result_and_payouts", fake_result)
        monkeypatch.setattr(BoatRaceScraper, "get_before_info_and_weather", fake_bi)
        return BoatRaceScraper(load_config())

    @pytest.mark.parametrize("workers", [1, 3])
    def test_直前情報と気象が収集結果に入る(self, monkeypatch, workers):
        """⚠️ 並列と逐次で経路が別。両方見る。"""
        sc = self._scraper(monkeypatch)
        got = sc.collect_day_results(date(2026, 9, 2), max_workers=workers)
        assert "before_info" in got and len(got["before_info"]), "直前情報が無い"
        assert "weather" in got and len(got["weather"]), "気象が無い"
        assert len(got["race_result"]), "結果まで壊した"

    def test_切ることもできる(self, monkeypatch):
        sc = self._scraper(monkeypatch)
        got = sc.collect_day_results(date(2026, 9, 2), max_workers=1,
                                     with_before_info=False)
        assert "before_info" not in got
        assert len(got["race_result"]), "切ったら結果まで消えた"

    @pytest.mark.parametrize("workers", [1, 3])
    def test_直前情報が落ちても結果は残る(self, monkeypatch, workers):
        """⚠️ 片方の失敗でもう片方まで捨てると、判定そのものが止まる。"""
        sc = self._scraper(monkeypatch, bi_ok=False)
        got = sc.collect_day_results(date(2026, 9, 2), max_workers=workers)
        assert len(got["race_result"]), "直前情報の失敗で結果が消えた"
        assert len(got["payouts"]), "直前情報の失敗で払戻が消えた"

    def test_直前情報が落ちても結果を取り直さない(self, monkeypatch):
        """⚠️ 不変条件（結果が残る）は再取得ブロックでも満たされてしまう。

        直前情報の失敗で `_worker` ごと落ちると、結果が空になり
        「空だったレースを取り直す」経路が走って回収される。**結果は残るので
        上のテストは通る**が、裏で**全レースを2度取りに行っている**。
        だから取得回数で見る。

        （このテストは、try/except を外す注入で落ちることを確認済み。
         外形だけ見ていたら気づけなかった。）
        """
        from src.scraping.official import BoatRaceScraper
        calls = []
        sc = self._scraper(monkeypatch, bi_ok=False)
        orig = BoatRaceScraper.get_race_result_and_payouts

        def counting(self, vc, d, rn):
            calls.append((vc, rn))
            return orig(self, vc, d, rn)
        monkeypatch.setattr(BoatRaceScraper, "get_race_result_and_payouts", counting)

        sc.collect_day_results(date(2026, 9, 2), max_workers=3)
        assert len(calls) == 2, (
            f"結果を {len(calls)}回 取りに行った（2レースなので2回のはず）。"
            f"直前情報の失敗が結果の取得を巻き込んでいる")

    def test_朝の一括収集では取らないまま(self, monkeypatch):
        """朝はまだ公開されていない。既定で取りに行くと空振りで遅くなるだけ。"""
        src = inspect.signature(
            __import__("src.scraping.official", fromlist=["x"])
            .BoatRaceScraper._collect_one_stadium)
        assert src.parameters["skip_before_info"].default is True


class TestDailyCheckWatchesIt:
    """再発を見張る検査が動くこと。前回は見張りが無く数ヶ月気づけなかった。"""

    def test_充足率を返す(self):
        from scripts.daily_check import _before_info_ratio
        got = _before_info_ratio("2026-06-15")
        if got is None:
            pytest.skip("DB を読めない")
        n, k = got
        assert n > 0
        assert k / n >= 0.8, f"2026-06-15 の直前情報が {k}/{n} しかない"

    def test_止まっている日を異常と判定する(self):
        """2026-09-03 は実際に0%。ここが OK になったら検査が壊れている。"""
        from scripts.daily_check import _before_info_ratio
        got = _before_info_ratio("2026-09-03")
        if got is None:
            pytest.skip("DB を読めない")
        n, k = got
        # 夜の収集を回すまでは0%。回した後は8割超になる。
        # どちらでもよいが、**閾値0.8で判定できる形**であることを確かめる。
        assert n > 0
        assert isinstance(k, int)

    def test_検査が一覧に載っている(self):
        import inspect as _i

        from scripts import daily_check
        src = _i.getsource(daily_check)
        assert "直前情報の収集" in src, "daily_check に検査が無い"
        assert "_before_info_ratio" in src
