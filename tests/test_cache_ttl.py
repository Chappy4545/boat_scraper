"""当日のHTMLが古いまま返っていないかを見る。

2026-08-30 の実害
-----------------
    16:00  pay ページを取得。そのとき終了済みだったのは 121 レース
    22:53  夜間処理が同じURLを引く → **キャッシュから 121 件が返る**
           実際には 168 レース終了済み
    結果   結果収集 119/168、判定 16/20。エラーは出ず exit 0

TTL が全ページ一律24時間だったのが原因。さらにキャッシュ削除は処理の
最後に走るので、途中で落ちた run は古い写しを残し、**やり直したいときに
限って**それを読む。

当日のページを短命にすれば、削除の位置に関係なく直る。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.utils.cache import FileCache

URL = "https://www.boatrace.jp/owpc/pc/race/pay"


def _cache(tmp_path, **kw) -> FileCache:
    return FileCache(cache_dir=str(tmp_path), **kw)


def test_当日のページは半日たてば取り直す(tmp_path, monkeypatch):
    """08-30 の再現。16時の写しが22時に返ってはいけない。"""
    today = datetime(2026, 8, 30, 16, 0)
    c = _cache(tmp_path)
    params = {"hd": "20260830"}

    monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(today))
    c.set(URL, "終了済み121レース", params)
    assert c.get(URL, params) == "終了済み121レース", "直後は使ってよい"

    monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(today + timedelta(hours=6, minutes=53)))
    assert c.get(URL, params) is None, \
        "22:53 に 16:00 の写しを返した（08-30 と同じ壊れ方）"


def test_過去日のページは使い回す(tmp_path, monkeypatch):
    """結果も払戻も確定しているので取り直す意味がない。

    ここを短命にすると、履歴の取り込みで同じページを何百回も取りに行く。
    """
    c = _cache(tmp_path)
    params = {"hd": "20260829"}      # 前日
    monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(datetime(2026, 8, 30, 16, 0)))
    c.set(URL, "確定した結果", params)
    monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(datetime(2026, 8, 30, 22, 53)))
    assert c.get(URL, params) == "確定した結果"


def test_当日でも数分以内なら使う(tmp_path, monkeypatch):
    """1回の処理の中で同じページを何度も引く。ここまで潰すと取得が数倍になる。"""
    t = datetime(2026, 8, 30, 16, 0)
    c = _cache(tmp_path)
    params = {"hd": "20260830"}
    monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(t))
    c.set(URL, "x", params)
    monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(t + timedelta(minutes=2)))
    assert c.get(URL, params) == "x"


def test_日付が読めないページは短命側に倒す(tmp_path, monkeypatch):
    """判断できないときに長命へ倒すと、変わるページを古いまま返す。"""
    t = datetime(2026, 8, 30, 16, 0)
    c = _cache(tmp_path)
    for params in ({}, {"hd": "not-a-date"}, {"jcd": "01"}, None):
        monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(t))
        c.set(URL, "x", params)
        monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(t + timedelta(hours=1)))
        assert c.get(URL, params) is None, f"params={params} を長命扱いしている"


def test_live_ttl_0なら当日を保存しない(tmp_path, monkeypatch):
    t = datetime(2026, 8, 30, 16, 0)
    c = _cache(tmp_path, live_ttl_minutes=0)
    monkeypatch.setattr("src.utils.cache.datetime", _FrozenAt(t))
    c.set(URL, "x", {"hd": "20260830"})
    assert c.get(URL, {"hd": "20260830"}) is None
    # 過去日は保存される
    c.set(URL, "y", {"hd": "20260829"})
    assert c.get(URL, {"hd": "20260829"}) == "y"


@pytest.mark.parametrize("html", [
    "<html>\r\n<body>\r\n改行がCRLF\r\n</body>\r\n",
    "<html>\n<body>\nLFだけ\n</body>\n",
    "混在\r\nと\nと素の\rと",
    "改行なし",
])
def test_キャッシュを通しても文字列が変わらない(tmp_path, html):
    """⚠️ 2026-08-31 実測: pay ページの CRLF 17箇所が `\\n\\n` に化けていた。

    Path.write_text は Windows で `\\n`→`\\r\\n` に変換し、read_text は
    `\\r\\n`→`\\n` に戻す。元が `\\r\\n` だと `\\r\\r\\n`→`\\n\\n` になる。
    「取得したHTML」と「キャッシュのHTML」が別物になると、キャッシュに
    当たったかどうかで結果が変わりうる。
    """
    c = _cache(tmp_path)
    params = {"hd": "20260829"}
    c.set(URL, html, params)
    assert c.get(URL, params) == html


def test_メタが壊れていたら取り直す(tmp_path):
    c = _cache(tmp_path)
    params = {"hd": "20260829"}
    c.set(URL, "x", params)
    key = c._key(URL, params)
    c._meta_path(key).write_text("{壊れている", encoding="utf-8")
    assert c.get(URL, params) is None


def test_設定が実際に効いている():
    """config を読んだスクレイパーが短命TTLを持っていること。

    cache.py だけ直しても、base.py が既定値で作っていたら意味がない。
    """
    from src.scraping.base import BaseScraper
    from src.utils.helpers import load_config
    s = BaseScraper(load_config())
    assert s.cache.live_ttl < s.cache.ttl, \
        "当日のページが過去日と同じ寿命になっている"
    assert s.cache.live_ttl <= timedelta(minutes=15), \
        f"当日のページの寿命が長すぎる: {s.cache.live_ttl}"


class _FrozenAt:
    """datetime.now() だけ固定する差し替え。他の使い方はそのまま通す。"""

    def __init__(self, t: datetime):
        self._t = t

    def now(self, tz=None):
        return self._t

    def __getattr__(self, name):
        return getattr(datetime, name)


@pytest.fixture(autouse=True)
def _restore():
    yield
