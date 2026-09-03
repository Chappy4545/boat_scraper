"""範囲表記のオッズ（複勝・拡連複）で上限を落としていないかを見る。

2026-09-03 に見つかった取りこぼし
---------------------------------
複勝と拡連複の板は `1.0-1.2` のような**範囲**で出る。「誰と一緒に2着（3着）
以内へ入るか」で配当が変わるため。コードは**下限だけ**を読んでいて、上限は
毎日捨てていた。オッズは遡って取れないので、捨てた分は永久に戻らない。

    実測（8/31-9/3 の当たり269本）: 拡連複の 55.0% が下限より高く払っている
                                   中央で1.09倍 → EV が系統的に低く出る

さらに複勝は収集のバケット自体に無く、`odds` テーブルに**0件**だった
（単勝は50,503件）。単勝と同じ oddstf ページなので通信は増えないのに、
作っていなかっただけ。

⚠️ 上限があっても「元返しか否か」は買う時点では確定しない（相方の艇で決まる）。
   実測では下限1.0のうち実払戻が元返しなのは60.4%で、最大17.9倍まで出ている。
   範囲の幅は情報であって予告ではない。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scraping.official import _parse_odds_range  # noqa: E402


class TestParseRange:
    def test_範囲は下限と上限に割れる(self):
        assert _parse_odds_range("1.0-1.2") == (1.0, 1.2)
        assert _parse_odds_range("11.8-12.6") == (11.8, 12.6)

    def test_単一値は下限と上限が同じ(self):
        """範囲でない賭式でも同じ関数を通せるように。"""
        assert _parse_odds_range("2.4") == (2.4, 2.4)

    def test_読めない値はNoneで捨てられる(self):
        for bad in ("", "   ", "欠場", "abc", "-"):
            assert _parse_odds_range(bad) == (None, None), bad

    def test_0以下は捨てる(self):
        """0 を残すと 1/odds で落ちる、あるいは EV が無限大になる。"""
        assert _parse_odds_range("0.0-1.0") == (None, None)
        assert _parse_odds_range("0") == (None, None)

    def test_上限は必ず下限以上(self):
        for txt in ("1.0-1.2", "2.4", "1.6-1.9-2.2", "3.0-3.0"):
            lo, hi = _parse_odds_range(txt)
            assert lo is not None and hi is not None
            assert hi >= lo, txt


class TestScraperEmitsUpper:
    """パーサが odds_upper 列を作ること。落とすと静かに情報が消える。"""

    FUKUSHO_HTML = """
    <html><body>
      <table><tr><td>締切</td></tr></table>
      <table>
        <tr><td>1</td><td>選手A</td><td>4.2</td></tr>
        <tr><td>2</td><td>選手B</td><td>2.2</td></tr>
      </table>
      <table>
        <tr><td>1</td><td>選手A</td><td>2.5-3.7</td></tr>
        <tr><td>2</td><td>選手B</td><td>1.0-1.2</td></tr>
        <tr><td>3</td><td>選手C</td><td></td></tr>
      </table>
    </body></html>
    """

    def _scraper(self):
        from src.scraping.official import BoatRaceScraper
        from src.utils.helpers import load_config
        return BoatRaceScraper(load_config())

    def test_複勝が下限と上限の両方を返す(self):
        sc = self._scraper()
        df = sc._parse_odds_fukusho(self.FUKUSHO_HTML, "02", date(2026, 9, 3), 1)
        assert not df.empty
        assert "odds_upper" in df.columns, "上限の列が無い（毎日捨てることになる）"
        r = df[df.combination == "1"].iloc[0]
        assert (r.odds, r.odds_upper) == (2.5, 3.7)
        r = df[df.combination == "2"].iloc[0]
        assert (r.odds, r.odds_upper) == (1.0, 1.2)

    def test_複勝の空欄の行は落ちる(self):
        sc = self._scraper()
        df = sc._parse_odds_fukusho(self.FUKUSHO_HTML, "02", date(2026, 9, 3), 1)
        assert "3" not in set(df.combination), "オッズ空欄の艇を拾っている"

    def test_単勝表を複勝として読んでいない(self):
        """table[1]=単勝 / table[2]=複勝。取り違えると値が丸ごと入れ替わる。"""
        sc = self._scraper()
        f = sc._parse_odds_fukusho(self.FUKUSHO_HTML, "02", date(2026, 9, 3), 1)
        t = sc._parse_odds_tansho(self.FUKUSHO_HTML, "02", date(2026, 9, 3), 1)
        assert float(t[t.combination == "1"].iloc[0].odds) == 4.2
        assert float(f[f.combination == "1"].iloc[0].odds) == 2.5

    def test_拡連複が下限と上限の両方を返す(self):
        html = """
        <html><body>
          <table><tr><td>締切</td></tr></table>
          <table>
            <tr><th>見出し</th></tr>
            <tr><td>2</td><td>1.6-1.9</td></tr>
            <tr><td>3</td><td>1.9-2.3</td><td>3</td><td>3.6-4.8</td></tr>
          </table>
        </body></html>
        """
        sc = self._scraper()
        df = sc._parse_odds_kakurenfuku(html, "02", date(2026, 9, 3), 1)
        assert "odds_upper" in df.columns, "上限の列が無い"
        got = {r.combination: (r.odds, r.odds_upper) for r in df.itertuples()}
        assert got["1-2"] == (1.6, 1.9)
        assert got["1-3"] == (1.9, 2.3)
        assert got["2-3"] == (3.6, 4.8)


class TestCollectionIncludesFukusho:
    """収集が実際に複勝を返すこと。

    ⚠️ 最初この節を「ソースに "odds_fukusho" が出てくるか」で書いたら、
    バケットの定義を消しても append 行が残るせいで**通ってしまった**。
    バグを入れて落ちることを確かめる手順で気づいた。だから振る舞いで見る。
    """

    def _stub_scraper(self, monkeypatch):
        """通信せずに oddstf / その他ページを返すスクレイパ。"""
        from src.scraping.official import BoatRaceScraper
        from src.utils.helpers import load_config

        oddstf = TestScraperEmitsUpper.FUKUSHO_HTML
        sc = BoatRaceScraper(load_config())

        def fake_fetch(url, params=None, **kw):
            return oddstf if url.endswith("oddstf") else "<html></html>"

        monkeypatch.setattr(sc, "_fetch_raw", fake_fetch)
        return sc

    def test_収集結果に複勝が入る(self, monkeypatch):
        """2026-09-03 まで無く、odds テーブルに複勝が0件だった。"""
        sc = self._stub_scraper(monkeypatch)
        got = sc._collect_one_stadium(date(2026, 9, 3), "02",
                                      skip_odds=False, skip_before_info=True,
                                      skip_results=True)
        assert "odds_fukusho" in got, "収集に複勝のバケットが無い"
        frames = [f for f in got["odds_fukusho"] if f is not None and not f.empty]
        assert frames, "複勝が1件も収集されていない（板が毎日消える）"
        df = frames[0]
        assert "odds_upper" in df.columns
        assert set(df.bet_type) == {"fukusho"}

    def test_収集結果に単勝も残っている(self, monkeypatch):
        """複勝を足すときに単勝を壊していないこと（同じページを共有する）。"""
        sc = self._stub_scraper(monkeypatch)
        got = sc._collect_one_stadium(date(2026, 9, 3), "02",
                                      skip_odds=False, skip_before_info=True,
                                      skip_results=True)
        frames = [f for f in got["odds_tansho"] if f is not None and not f.empty]
        assert frames, "単勝が壊れた"
        assert float(frames[0].iloc[0].odds) == 4.2

    def test_束ねる側にも複勝の受け皿がある(self, monkeypatch):
        """`collect_day` の merged に無いと KeyError で落ちる。"""
        import inspect as _inspect

        from src.scraping.official import BoatRaceScraper
        merged_src = _inspect.getsource(BoatRaceScraper.collect_day)
        assert '"odds_fukusho"' in merged_src, "merged に受け皿が無い（KeyError）"

    def test_保存の一覧に複勝がある(self):
        """バケットを作っても save_day が呼ばなければ DB に入らない。"""
        import inspect as _inspect

        from src.ingestion import saver
        src = _inspect.getsource(saver.save_day)
        assert '"odds_fukusho"' in src, "save_day が複勝を保存していない"

    def test_複勝は単勝と同じページで通信を増やさない(self, monkeypatch):
        """別ページを叩き始めると収集時間が伸びる。取得回数で見る。"""
        from src.scraping.official import BoatRaceScraper
        from src.utils.helpers import load_config

        seen = []
        sc = BoatRaceScraper(load_config())

        def fake_fetch(url, params=None, **kw):
            seen.append(url)
            return (TestScraperEmitsUpper.FUKUSHO_HTML
                    if url.endswith("oddstf") else "<html></html>")

        monkeypatch.setattr(sc, "_fetch_raw", fake_fetch)
        sc._collect_one_stadium(date(2026, 9, 3), "02", skip_odds=False,
                                skip_before_info=True, skip_results=True)
        tf = [u for u in seen if u.endswith("oddstf")]
        assert len(tf) == 12, f"oddstf を12レース分より多く叩いている: {len(tf)}回"


class TestMigration:
    """`create_all` は列を足さない。既存の424MB DBに列を入れる経路を見る。"""

    def _old_shape_db(self, tmp_path):
        import sqlite3
        p = tmp_path / "old.db"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE odds (id INTEGER PRIMARY KEY, race_id INT, "
                  "bet_type TEXT, combination TEXT, odds FLOAT, "
                  "is_final BOOLEAN, is_live BOOLEAN, recorded_at DATETIME)")
        c.execute("INSERT INTO odds (race_id,bet_type,combination,odds) "
                  "VALUES (1,'fukusho','1',1.5)")
        c.commit()
        c.close()
        return p

    def test_古い形のDBに列が足される(self, tmp_path):
        from sqlalchemy import create_engine, inspect as sa_inspect
        from src.ingestion.database import _add_missing_columns

        p = self._old_shape_db(tmp_path)
        eng = create_engine(f"sqlite:///{p}")
        assert "odds_upper" not in {c["name"] for c in sa_inspect(eng).get_columns("odds")}
        _add_missing_columns(eng)
        assert "odds_upper" in {c["name"] for c in sa_inspect(eng).get_columns("odds")}

    def test_二度実行しても壊れない(self, tmp_path):
        """毎回の init_db で走るので、冪等でないと起動が落ちる。"""
        from sqlalchemy import create_engine, text
        from src.ingestion.database import _add_missing_columns

        p = self._old_shape_db(tmp_path)
        eng = create_engine(f"sqlite:///{p}")
        _add_missing_columns(eng)
        _add_missing_columns(eng)
        with eng.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM odds")).scalar() == 1

    def test_既存の行は消えない(self, tmp_path):
        from sqlalchemy import create_engine, text
        from src.ingestion.database import _add_missing_columns

        p = self._old_shape_db(tmp_path)
        eng = create_engine(f"sqlite:///{p}")
        _add_missing_columns(eng)
        with eng.connect() as c:
            row = c.execute(text("SELECT odds, odds_upper FROM odds")).one()
        assert row[0] == 1.5 and row[1] is None


class TestArchiveCarriesUpper:
    """退避JSONで落とすと、その日の上限は永久に失われる。"""

    def _data(self):
        import pandas as pd
        return {
            "racelist": pd.DataFrame([{"x": 1}]),        # オッズでないものは無視
            "odds_fukusho": pd.DataFrame([
                {"stadium_code": "02", "race_no": 1, "bet_type": "fukusho",
                 "combination": "1", "odds": 2.5, "odds_upper": 3.7},
            ]),
            "odds_tansho": pd.DataFrame([
                {"stadium_code": "02", "race_no": 1, "bet_type": "tansho",
                 "combination": "1", "odds": 4.2},
            ]),
        }

    def test_上限が退避行に入る(self):
        import main
        rows = main._archive_odds_rows(self._data())
        f = [r for r in rows if r["bet_type"] == "fukusho"]
        assert f, "複勝が退避されていない"
        assert f[0].get("odds_upper") == 3.7, "上限が落ちている（永久に戻らない）"

    def test_範囲でない賭式には上限を付けない(self):
        import main
        rows = main._archive_odds_rows(self._data())
        t = [r for r in rows if r["bet_type"] == "tansho"]
        assert t and "odds_upper" not in t[0]

    def test_オッズ以外は退避しない(self):
        import main
        rows = main._archive_odds_rows(self._data())
        assert {r["bet_type"] for r in rows} == {"fukusho", "tansho"}

    def test_予備経路も上限を運ぶ(self):
        """ローカルが落ちた日に動く archive_odds 側。"""
        import inspect as _inspect

        import main
        src = _inspect.getsource(main.cmd_archive_odds)
        assert 'row["odds_upper"] = float(up)' in src, "予備経路が上限を落としている"


class TestPersistUpper:
    """DB に上限が実際に残ること。列が無いと saver は例外を握りつぶす。"""

    def test_保存して読み直すと上限が残る(self, tmp_path, monkeypatch):
        import pandas as pd
        from sqlalchemy import text

        import src.ingestion.database as db
        from src.ingestion.saver import save_odds
        from src.utils.helpers import load_config

        cfg = load_config()
        cfg = {**cfg, "database": {**cfg["database"],
                                   "url": f"sqlite:///{tmp_path/'t.db'}"}}
        db.init_db(cfg)
        n = save_odds(pd.DataFrame([
            {"stadium_code": "02", "race_date": date(2026, 9, 3), "race_no": 1,
             "bet_type": "fukusho", "combination": "1",
             "odds": 2.5, "odds_upper": 3.7},
        ]), is_final=False, force_live=True)
        assert n == 1
        with db.get_engine().connect() as c:
            row = c.execute(text(
                "SELECT odds, odds_upper FROM odds WHERE bet_type='fukusho'")).one()
        assert (row[0], row[1]) == (2.5, 3.7)

    def test_本番DBに列がある(self):
        from sqlalchemy import inspect as sa_inspect

        import src.ingestion.database as db
        from src.utils.helpers import load_config
        try:
            db.init_db(load_config())
            cols = {c["name"] for c in sa_inspect(db.get_engine()).get_columns("odds")}
        except Exception as e:
            pytest.skip(f"DB を読めない: {str(e)[:60]}")
        assert "odds_upper" in cols, "既存DBに列が入っていない（移行が動いていない）"
