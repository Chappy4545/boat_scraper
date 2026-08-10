"""バックテスト/予測の未来情報リーク防止インバリアント。

過去に `_load_odds` が payouts へフォールバックしていた。payouts は
的中組合せしか持たないため、外れ目は「オッズなし→見送り」となり、
結果として「的中目だけを買える」状態になっていた（回収率が実態より
大幅に良く見える）。同じ穴を二度と開けないための回帰テスト。

実行: python -m pytest tests/test_backtest_no_leak.py -v
"""
from __future__ import annotations

import os

import pytest

DB_PATH = os.path.join("data", "boatrace.db")
pytestmark = pytest.mark.skipif(
    not os.path.exists(DB_PATH), reason="boatrace.db がない環境ではスキップ"
)


@pytest.fixture(scope="module")
def engine():
    from src.ingestion.database import get_engine

    return get_engine()


def _scalar(engine, sql: str):
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text(sql)).fetchone()
    return row[0] if row else None


def test_load_odds_does_not_fall_back_to_payouts(engine):
    """payouts はあるが odds が無いレースで、必ず空を返すこと。"""
    from src.backtest.runner import _load_odds

    race_id = _scalar(
        engine,
        """SELECT py.race_id FROM payouts py
           WHERE NOT EXISTS (SELECT 1 FROM odds o WHERE o.race_id = py.race_id)
           LIMIT 1""",
    )
    if race_id is None:
        pytest.skip("payouts のみのレースが存在しない")

    df = _load_odds(engine, int(race_id))
    assert df.empty, (
        f"race_id={race_id} は odds を持たないのに {len(df)} 行返した。"
        " payouts フォールバックが復活している（未来情報リーク）。"
    )


def test_backtest_targets_only_races_with_final_odds(engine):
    """バックテスト対象は確定オッズと結果が揃うレースに限ること。"""
    from src.backtest.runner import _load_backtest_race_ids

    rows = _load_backtest_race_ids(engine, "2026-07-11", "2026-08-01")
    if not rows:
        pytest.skip("対象期間にレースがない")

    from sqlalchemy import text

    ids = [int(r[0]) for r in rows[:50]]  # 先頭50件を検証
    with engine.connect() as conn:
        for rid in ids:
            n_odds = conn.execute(
                text("SELECT COUNT(*) FROM odds WHERE race_id=:r AND is_final=1"),
                {"r": rid},
            ).fetchone()[0]
            n_res = conn.execute(
                text("SELECT COUNT(*) FROM race_results WHERE race_id=:r"), {"r": rid}
            ).fetchone()[0]
            assert n_odds > 0, f"race_id={rid} に確定オッズが無いのに対象化された"
            assert n_res > 0, f"race_id={rid} に結果が無いのに対象化された"
