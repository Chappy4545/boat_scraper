"""同じ鍵が2回来ても保存が全滅しないこと。**全saverを一度に見る。**

このバグは2回起きている
-----------------------
    2026-08-30  払戻。UNIQUE違反でその日の保存が全滅し、終了コードは0だった。
                拡連複は当日しか取れないので危なかった
    2026-09-03  直前情報。バックフィルが 37% から一歩も進まず、ログに
                `UNIQUE constraint failed: before_info.race_id, before_info.boat_no`。
                8月に払戻で直したのに、**こちらの saver には入れていなかった**

仕組み
------
セッションは `autoflush=False` なので、直前に `session.add` した行は
`session.query(...).first()` に**引っかからない**。同じ鍵が2回来ると
2行 add され、**セッションを抜けるとき**に UNIQUE 制約で落ちる。

    落ちるのはループの外 → 行ごとの except では捕まらない
    → そのバッチが丸ごと失われる → しかも終了コードは0

⚠️ だから「1つ直す」ではなく「**UNIQUE制約のある全saverを見張る**」形にする。
7個目が生まれたときにここで落ちること。
"""
from __future__ import annotations

import re
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# UNIQUE 制約のあるテーブルを書く saver と、その鍵
GUARDED = {
    "save_racelist": "race_entries(race_id, boat_no)",
    "save_before_info": "before_info(race_id, boat_no)",
    "save_weather": "weather(race_id)",
    "save_odds": "odds(race_id, bet_type, combination, is_final)",
    "save_race_result": "race_results(race_id, arrival_order)",
    "save_payouts": "payouts(race_id, bet_type, combination)",
}


def _fresh_db():
    """本番DBを触らないよう、使い捨てDBに繋ぎ直す。"""
    import src.ingestion.database as db
    from src.utils.helpers import load_config
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    db.init_db({**cfg, "database": {**cfg["database"], "url": f"sqlite:///{tmp}"}})
    return db


BASE = {"stadium_code": "01", "race_date": date(2026, 9, 2), "race_no": 1}

ROWS = {
    "save_racelist": {**BASE, "boat_no": 1, "racer_no": 1000, "racer_name": "選手"},
    "save_before_info": {**BASE, "boat_no": 1, "exhibition_time": 6.7},
    "save_weather": {**BASE, "weather": "晴", "wind_speed": 3},
    "save_odds": {**BASE, "bet_type": "nirenfuku", "combination": "1-2", "odds": 9.0},
    "save_race_result": {**BASE, "boat_no": 1, "arrival_order": 1},
    "save_payouts": {**BASE, "bet_type": "nirenfuku", "combination": "1-2",
                     "payout": 900},
}


class TestEverySaverSurvivesDuplicates:
    @pytest.mark.parametrize("name", sorted(GUARDED))
    def test_同じ行が2回来ても落ちない(self, name):
        """⚠️ これが本体。重複よけが無いと**ここで例外**になる。"""
        db = _fresh_db()
        from src.ingestion import saver
        fn = getattr(saver, name)
        row = ROWS[name]
        df = pd.DataFrame([row, dict(row)])       # まったく同じ行を2つ
        try:
            fn(df)
        except Exception as e:                     # noqa: BLE001
            pytest.fail(f"{name} が重複で落ちた: {type(e).__name__}: {str(e)[:120]}")

    @pytest.mark.parametrize("name", sorted(GUARDED))
    def test_重複があっても中身は保存される(self, name):
        """落ちないだけでなく、1行はちゃんと入ること（黙って捨てない）。"""
        db = _fresh_db()
        from sqlalchemy import text

        from src.ingestion import saver
        table = GUARDED[name].split("(")[0]
        row = ROWS[name]
        getattr(saver, name)(pd.DataFrame([row, dict(row)]))
        with db.get_engine().connect() as c:
            n = c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        assert n == 1, f"{name}: {table} に {n}行（1行のはず）"

    @pytest.mark.parametrize("name", sorted(GUARDED))
    def test_重複の後ろに続く行も失われない(self, name):
        """⚠️ 実害はここ。重複1件でバッチ全体が消えるのが本当の被害。"""
        db = _fresh_db()
        from sqlalchemy import text

        from src.ingestion import saver
        table = GUARDED[name].split("(")[0]
        row = ROWS[name]
        # 重複2件のあとに、別のレースの行を置く
        other = {**row, "race_no": 2}
        getattr(saver, name)(pd.DataFrame([row, dict(row), other]))
        with db.get_engine().connect() as c:
            n = c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        assert n == 2, (
            f"{name}: {table} に {n}行（2行のはず）。"
            f"重複の巻き添えで後続が失われている")


class TestNoSeventhOffender:
    """7個目が生まれたら気づけるようにする。"""

    def test_UNIQUE制約のあるsaverはすべて重複よけを持つ(self):
        src = (ROOT / "src" / "ingestion" / "saver.py").read_text(encoding="utf-8")
        bodies = dict(re.findall(r"def (save_\w+)\(.*?\n(.*?)(?=\ndef |\Z)", src, re.S))
        missing = [n for n in GUARDED
                   if n in bodies and "seen" not in bodies[n]]
        assert not missing, f"重複よけが無い saver: {missing}"

    def test_宣言だけで使っていないものが無い(self):
        """⚠️ 2026-09-03、save_racelist に seen を宣言して**使い忘れた**。
        変数はあるので上のテストは通ってしまう。実際に参照しているかを見る。
        """
        src = (ROOT / "src" / "ingestion" / "saver.py").read_text(encoding="utf-8")
        bodies = dict(re.findall(r"def (save_\w+)\(.*?\n(.*?)(?=\ndef |\Z)", src, re.S))
        dead = [n for n in GUARDED
                if n in bodies and "seen" in bodies[n]
                and "in seen" not in bodies[n]]
        assert not dead, f"seen を宣言しただけで使っていない saver: {dead}"
