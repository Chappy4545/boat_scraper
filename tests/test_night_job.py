"""夜間処理の .bat が「黙って死なない」形になっているかを見る。

このプロジェクトの故障は「エラーを出さずに何もしない」形が圧倒的に多く、
その温床が夜間処理の .bat だった。ここで守るのは3つ。

1. **非ASCIIを入れない**
   cmd.exe はコンソールのコードページで .bat を読む。UTF-8 の日本語
   コメントを入れると化けて「'??' is not recognized」で行が飛ぶ。
   2026-08-31 に検証用の .bat で実際に再現した。

2. **失敗しても点検と push まで到達する**
   以前は judge が失敗すると `exit /b 1` でそこで終わっており、
   **一番知りたい「失敗した日」だけ** health.json が更新されず、
   commit もされなかった。画面には前日の「すべて正常」が残る。

3. **終了マーカーを必ず書く**
   daily_check は「start があって done が無い」実行を"途中で殺された"と
   判定する（2026-08-28 がこれで3日間気づかれなかった）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATS = sorted((ROOT / "scripts").glob("*.bat"))


@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_batは非ASCIIを含まない(bat: Path):
    raw = bat.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not bad, (
        f"{bat.name} に非ASCIIが {len(bad)} バイト（最初の位置 {bad[0][0]}）。"
        f"cmd.exe がコードページ違いで化けて読み、その行が飛ぶ")


def _code() -> str:
    """コメント(REM)を落とした実行行だけ。

    コメント本文に "exit /b 1" と書くことがあるので、素の文字列検索だと
    自分の説明文に引っかかる（2026-08-31 に実際に引っかかった）。
    """
    src = (ROOT / "scripts" / "daily_judge.bat").read_text(encoding="ascii")
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().upper().startswith("REM"))


def test_judgeは失敗しても点検まで到達する():
    """`exit /b 1` が daily_check より前に無いこと。"""
    code = _code()
    assert "exit /b 1" not in code[:code.index("daily_check.py")], (
        "daily_check.py より前で打ち切っている。失敗した日ほど "
        "health.json が更新されず、画面は前日の『すべて正常』のままになる")


def test_judgeは失敗しても記録をpushする():
    code = _code()
    assert "exit /b 1" not in code[:code.rindex("git push")], \
        "push より前で打ち切っている。点検結果が端末に届かない"


def test_judgeは必ず終了マーカーを書く():
    """done / failed のどちらかを必ず書くこと。

    書かずに終わると daily_check が「途中で殺された」と判定する
    （＝本当に殺された回と区別がつかなくなる）。
    """
    code = _code()
    assert "JUDGE done" in code and "JUDGE failed" in code
    # 失敗マーカーは exit の直前にあること
    i_failed = code.index("JUDGE failed")
    i_exit = code.index("exit /b 1", i_failed)
    assert 0 < i_exit - i_failed < 120, \
        "JUDGE failed を書かずに exit している経路がある"
