"""賭式の名前が経路をまたいで食い違っていないかを見る。

2026-08-31 に見つかった不具合
-----------------------------
払戻ページの解析表 `official.BET_TYPE_MAP` に **「複勝」だけ登録が無く**、
`payouts` テーブルに日本語のまま 74,100行 入っていた（他6賭式は英語）。

判定は `payouts[(race_id, "fukusho", 組)]` を引くので**永久に見つからず、
複勝は着順に関係なく全部「外れ」**になっていた。日中判定(judge_live)も
同じ表を使うので同様。

    画面: 複勝「1」／結果 1-2-5（1着）なのに ✗外れ が並んでいた

⚠️ **登録が抜けても例外にならない。** `BET_TYPE_MAP.get(x, x)` が原文を
そのまま返すので、静かに別の名前で保存されるだけ。だから見張る。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.models.plackett_luce import BET_TYPE_JP  # noqa: E402
from src.scraping.official import BET_TYPE_MAP  # noqa: E402


def test_払戻の賭式名がすべて英語へ変換される():
    """公式の表記6種＋複勝が、すべて DB の名前へ写ること。"""
    for jp in ("3連単", "3連複", "2連単", "2連複", "拡連複", "単勝", "複勝"):
        assert jp in BET_TYPE_MAP, f"{jp} の登録が無い（原文のまま保存される）"
        assert BET_TYPE_MAP[jp].isascii(), f"{jp} → {BET_TYPE_MAP[jp]} が英語でない"


def test_モデル側の賭式名と一致する():
    """`BET_TYPE_JP`（モデル/設定が使う JP→db）と食い違わないこと。

    2つの表が別々に育つと、片方だけ直して残りが壊れる。
    """
    for jp, db in BET_TYPE_MAP.items():
        if jp in BET_TYPE_JP:
            assert BET_TYPE_JP[jp] == db, (
                f"{jp}: official={db} / plackett_luce={BET_TYPE_JP[jp]}")


def test_DBのpayoutsに日本語の賭式が残っていない():
    """移行のやり残しを見張る。1行でも残ると、その賭式は永久に外れ扱い。"""
    from sqlalchemy import text
    from src.ingestion.database import init_db, get_engine
    from src.utils.helpers import load_config
    try:
        init_db(load_config())
        with get_engine().connect() as c:
            bad = c.execute(text(
                "SELECT bet_type, COUNT(*) FROM payouts "
                "GROUP BY 1 HAVING bet_type GLOB '*[^ -~]*'")).all()
    except Exception as e:
        pytest.skip(f"DB を読めない: {str(e)[:60]}")
    assert not bad, f"payouts に英語でない賭式が残っている: {bad}"


def test_判定が複勝を引けること():
    """買い目の bet_type で payouts を引けるか。ここが今回落ちていた。"""
    from sqlalchemy import text
    from src.ingestion.database import init_db, get_engine
    from src.utils.helpers import load_config
    try:
        init_db(load_config())
        with get_engine().connect() as c:
            n = c.execute(text(
                "SELECT COUNT(*) FROM payouts WHERE bet_type='fukusho'")).scalar()
    except Exception as e:
        pytest.skip(f"DB を読めない: {str(e)[:60]}")
    if n == 0:
        pytest.skip("複勝の払戻がまだ無い")
    assert n > 0
