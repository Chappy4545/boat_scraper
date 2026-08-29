"""画面を直したのに端末へ届かない、が起きていないかを見る。

2026-08-29 に判明
----------------
`docs/sw.js` の静的アセットはキャッシュ優先で、`CACHE_NAME` を上げないと
端末は永久に古い JS/CSS を使い続ける。実際 sw.js は 08-16(v11) で止まり、
その間に app.js が4回更新されていた:

    08-23 前日実績が候補ルールを数えていた修正
    08-24 候補ルールを shrink_adj へ
    08-25 候補ルールを top1_value へ
    08-26 買い目タップ時の出走表・確率の修正

端末では候補ルールの除外が `rule === "market_blend"` のままだったため、
**買っていない候補（賭け金0）が買い目として並んでいた**（08-24〜28 で56行）。

v12 で stale-while-revalidate にしたので、上げ忘れても遅れは1回ぶんで済む。
それでも「上げ忘れ」は気づけないと困るので、ここで見張る。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SW = ROOT / "docs" / "sw.js"

# sw.js より新しくなってはいけないファイル（端末に配るもの）
SHIPPED = ["docs/js/app.js", "docs/css/style.css", "docs/index.html"]


def _git(*args) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _last_commit(path: str) -> str:
    return _git("log", "-1", "--format=%cI", "--", path)


def _dirty(path: str) -> bool:
    return bool(_git("diff", "--name-only", "HEAD", "--", path))


@pytest.fixture(scope="module")
def in_git_repo():
    if not _git("rev-parse", "--is-inside-work-tree"):
        pytest.skip("git リポジトリではない")


def test_cache_nameがある():
    m = re.search(r'CACHE_NAME\s*=\s*"boatrace-v(\d+)"', SW.read_text(encoding="utf-8"))
    assert m, "sw.js に CACHE_NAME が見つからない"
    assert int(m.group(1)) >= 12


@pytest.mark.parametrize("path", SHIPPED)
def test_配布物を直したらsw_jsも上げている(path, in_git_repo):
    """sw.js より新しい変更があってはいけない。

    片方だけコミットすると、その修正は端末に届かない。
    """
    if not (ROOT / path).exists():
        pytest.skip(f"{path} が無い")
    sw_dirty, target_dirty = _dirty("docs/sw.js"), _dirty(path)
    if target_dirty and not sw_dirty:
        pytest.fail(f"{path} を編集したが docs/sw.js が未編集。"
                    f"CACHE_NAME を上げないと端末に届かない")
    sw_t, t = _last_commit("docs/sw.js"), _last_commit(path)
    if not sw_t or not t:
        pytest.skip("コミット履歴なし")
    assert t <= sw_t, (
        f"{path} の最終更新 {t} が docs/sw.js の {sw_t} より新しい。"
        f"CACHE_NAME を上げ忘れている（端末に届かない）")


def test_静的アセットが取り直される():
    """キャッシュ優先のみだと更新が永久に届かない。裏で取り直すこと。"""
    src = SW.read_text(encoding="utf-8")
    tail = src[src.index("静的アセット"):]
    assert "fetch(req)" in tail and "c.put(req" in tail, \
        "静的アセットを裏で取り直していない（stale-while-revalidate になっていない）"


def test_dataはネットワーク優先のまま():
    """毎日変わるので、こちらをキャッシュ優先にしてはいけない。"""
    src = SW.read_text(encoding="utf-8")
    i = src.index('url.pathname.includes("/data/")')
    assert src.index("fetch(req)", i) < src.index("caches.match(req)", i)


def test_候補ルールの除外が画面側でも最新():
    """端末側の除外リストが古いと、買っていない候補が買い目として並ぶ。

    main.py の CANDIDATE_RULES と app.js の一覧が一致していること。
    """
    py = (ROOT / "main.py").read_text(encoding="utf-8")
    js = (ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8")
    m_py = re.search(r"^CANDIDATE_RULES\s*=\s*\(([^)]*)\)", py, re.M)
    m_js = re.search(r"^const CANDIDATE_RULES\s*=\s*\[([^\]]*)\]", js, re.M)
    assert m_py and m_js, "CANDIDATE_RULES が見つからない"
    names = lambda s: set(re.findall(r'"([^"]+)"', s))
    assert names(m_py.group(1)) == names(m_js.group(1)), \
        f"main.py {names(m_py.group(1))} と app.js {names(m_js.group(1))} がズレている"
