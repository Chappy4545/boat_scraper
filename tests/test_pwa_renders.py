"""PWA を実ブラウザで描画させ、JSON の中身どおり出ているかを見る。

なぜ要るか
----------
2026-08-30 に賭式を6つへ広げたが、画面には 2連複しか出ていなかった。
生成側（bets JSON）には 822本・6賭式すべて入っており、Python 側のテストは
全部通っていた。落ちていたのは `docs/js/app.js` の1行:

    state._betsCache = all.filter(b => !isCandidate(b));

isCandidate は「賭け金0なら候補」＝記録のみの5賭式を全部落とす。
「成績に入れるか」と「画面に出すか」を1つの関数で兼ねていたのが原因。

**JS を一度も実行していなかったから気づけなかった。**
Python のテストをいくら足してもこの種のバグには届かない。ここでブラウザに
描かせて、カードの枚数と賭式の種類を数える。

実体は scripts/pwa_smoke.py（手でも回せるようにしてある）。
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _load_smoke():
    spec = importlib.util.spec_from_file_location(
        "pwa_smoke", ROOT / "scripts" / "pwa_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def smoke():
    pytest.importorskip("playwright.sync_api", reason="playwright が無い")
    mod = _load_smoke()
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            p.chromium.launch(channel="chrome").close()
    except Exception as e:                      # ブラウザが無い環境（CI）
        pytest.skip(f"chrome を起動できない: {str(e)[:80]}")
    return mod


def _latest_day() -> str | None:
    """買い目が入っている直近の日。今日はまだ空のことがある。"""
    for i in range(0, 10):
        d = (date.today() - timedelta(days=i)).isoformat()
        p = DOCS / "data" / f"bets_{d}.json"
        if p.exists():
            try:
                if json.loads(p.read_text(encoding="utf-8")):
                    return d
            except Exception:
                pass
    return None


def test_画面が買い目JSONのとおり描画される(smoke):
    """カード枚数・賭式の種類・成績用の件数が JSON と一致すること。

    ⚠️ このテストは 2026-08-31 に「本物のバグを入れて落ちるか」を確かめてある。
    `_listCache` を `isPurchased` で作ると（＝当時の実装）
    「賭式 ['2連複'] / カード 35枚（812枚のはず）」で落ちる。
    """
    d = _latest_day()
    if not d:
        pytest.skip("買い目のある日が docs/data に無い")
    assert smoke.run(d, None) == 0, f"{d} の画面が JSON と一致しない（上の出力参照）"


def test_記録のみの賭式が画面から消えていない(smoke):
    """6賭式のうち、賭け金0のものが1つでも消えたら落ちる。

    件数一致より弱いが、意図（6賭式を見せる）を直接書いた形にしておく。
    件数だけだと「たまたま合っている」ことがある。
    """
    d = _latest_day()
    if not d:
        pytest.skip("買い目のある日が docs/data に無い")
    exp = smoke.expected_from_json(d)
    record_types = {b for b in exp["bet_types"]}
    if len(record_types) < 2:
        pytest.skip(f"{d} は賭式が {record_types} だけ（6賭式化の前）")
    assert smoke.run(d, None) == 0
