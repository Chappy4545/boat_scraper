"""確度の段階が、どの賭式でも意味を持つことを見張る。

2026-08-31〜09-03 に起きていたこと
----------------------------------
段階は「的中率70%以上ならS」という**絶対基準**だった。各賭式の的中率の
上限はこうなっている（未使用データ10,809レースの実測）:

    複勝 88.9% / 単勝 75.9% / 拡連複 67.7% /
    2連複 43.0% / 3連複 34.9% / 3連単 15.3%

つまり **下4つは永久にSにならない**。既定の一覧は「S だけ」を出す作りなので、
拡連複・3連複・3連単は**画面から丸ごと消えていた**（9/3 実測: 971件中
出ていたのは180件で、内訳は複勝113 + 単勝35 + 買った2連複32）。

上限15%〜89%の賭式に同じ物差しを当てれば当然そうなる、という種類のバグ。
段階を「その賭式の中での帯」に変えて直した。

⚠️ **hit（実測の的中率）の併記を外さないこと。**
段階が賭式内の順位になったので、記号だけ見ると「3連単のS」と「複勝のS」が
同じに見える。実際は 15% と 89% で別物。併記が唯一の歯止め。

このテストは JS を実行せず、app.js の定数を読んで性質を確かめる
（ブラウザを使う検査は tests/test_pwa_renders.py）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP = ROOT / "docs" / "js" / "app.js"
TYPES = ["fukusho", "tansho", "kakurenfuku",
         "nirenfuku", "sanrenfuku", "sanrentan"]


@pytest.fixture(scope="module")
def js() -> str:
    return APP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def confidence(js) -> dict:
    blk = re.search(r"const CONFIDENCE = \{(.+?)\n\};", js, re.S)
    assert blk, "CONFIDENCE を読めない"
    out = {}
    for bt, cuts, hits in re.findall(
            r"(\w+):\s*\{\s*cuts:\s*\[([^\]]+)\],\s*hit:\s*\[([^\]]+)\]",
            blk.group(1)):
        out[bt] = {"cuts": [float(x) for x in cuts.split(",")],
                   "hit": [float(x) for x in hits.split(",")]}
    return out


class TestTable:
    def test_6賭式すべてに帯がある(self, confidence):
        assert set(confidence) == set(TYPES), (
            f"抜けている: {set(TYPES) - set(confidence)}")

    def test_帯の数が揃っている(self, confidence):
        """cuts が3本、hit が4つ。ずれると confidenceOf が範囲外を読む。"""
        for bt, v in confidence.items():
            assert len(v["cuts"]) == 3, f"{bt}: cuts が {len(v['cuts'])}本"
            assert len(v["hit"]) == 4, f"{bt}: hit が {len(v['hit'])}個"

    def test_確率の切れ目が単調に上がる(self, confidence):
        for bt, v in confidence.items():
            assert v["cuts"] == sorted(v["cuts"]), f"{bt}: cuts が単調でない"

    def test_的中率が帯とともに上がる(self, confidence):
        """⭐ これが崩れたら、確率で並べる意味そのものが無い。"""
        for bt, v in confidence.items():
            assert v["hit"] == sorted(v["hit"]), (
                f"{bt}: 上の帯ほど当たる、が崩れている {v['hit']}")


class TestGradeIsWithinBetType:
    """⚠️ 本丸。段階が賭式ごとに閉じていること。"""

    def test_絶対基準の段階表が残っていない(self, js):
        """GRADE_BY_HIT が残っていたら、また賭式が消える。"""
        assert "GRADE_BY_HIT" not in js, (
            "絶対基準の段階表(GRADE_BY_HIT)が残っている。"
            "上限15%の3連単は永久にSにならず、既定の一覧から消える")
        assert "GRADE_BY_BAND" in js, "賭式内の段階表(GRADE_BY_BAND)が無い"

    def test_段階は帯の番号から決まる(self, js):
        """confidenceOf が hit の絶対値で段階を決めていないこと。"""
        fn = re.search(r"function confidenceOf\(bet\) \{(.+?)\n\}", js, re.S)
        assert fn, "confidenceOf を読めない"
        body = fn.group(1)
        assert "GRADE_BY_BAND[i]" in body, "段階が帯の番号から決まっていない"
        assert not re.search(r"hit\s*>=\s*0\.\d", body), (
            "confidenceOf が的中率の絶対値で段階を決めている")

    def test_全賭式で最上位帯に到達できる(self, confidence):
        """どの賭式にも S が存在すること＝既定の一覧に出られること。

        絶対基準に戻すと、拡連複(上限67.7%)・2連複(43.0%)・
        3連複(34.9%)・3連単(15.3%)がここで落ちる。
        """
        for bt, v in confidence.items():
            top = len(v["cuts"])           # confidenceOf の i が取りうる最大値
            assert top == 3, f"{bt}: 最上位帯の番号が {top}"
            assert v["hit"][top] > 0, f"{bt}: 最上位帯の的中率が 0"

    def test_既定の一覧が最上位帯で絞っている(self, js):
        fn = re.search(r"function isRecommended\(bet\) \{(.+?)\n\}", js, re.S)
        assert fn, "isRecommended を読めない"
        body = fn.group(1)
        assert "c.top" in body, "最上位帯で絞っていない"
        assert not re.search(r"hit\s*>=\s*0\.\d", body), (
            "既定の一覧が的中率の絶対値で絞っている（賭式が丸ごと消える）")

    def test_買った買い目は絞り込みで消えない(self, js):
        """自分が買った買い目が一覧から消えるのが一番まずい。"""
        fn = re.search(r"function isRecommended\(bet\) \{(.+?)\n\}", js, re.S)
        assert "isPurchased(bet)) return true" in fn.group(1), (
            "賭け金つきの買い目が無条件で通っていない")


class TestHitRateStaysVisible:
    """段階が賭式内の順位になったぶん、絶対値の併記が必須になった。"""

    def test_確度の印に実測の的中率が入る(self, js):
        fn = re.search(r"function confBadge\(bet\) \{(.+?)\n\}", js, re.S)
        assert fn, "confBadge を読めない"
        assert "c.hit" in fn.group(1), (
            "確度の印に実測の的中率が出ていない。"
            "記号だけだと 3連単のS(15%) と 複勝のS(89%) が同じに見える")

    def test_賭式ごとの実測回収率の表がある(self, js):
        """「S＝勝てる」と読ませないための併記。"""
        blk = re.search(r"const BET_TIER = \{(.+?)\n\};", js, re.S)
        assert blk, "BET_TIER を読めない"
        for bt in TYPES:
            assert bt in blk.group(1), f"BET_TIER に {bt} が無い"
        rois = [float(x) for x in re.findall(r"roi:\s*([\d.]+)", blk.group(1))]
        assert len(rois) == 6
        assert all(r < 100 for r in rois), (
            f"回収率100%以上の賭式がある: {rois}。"
            f"実測ではどれも100%未満なので、表示が事実とずれている")


class TestBuyFlagMatchesConfig:
    """⚠️ 画面の「実運用/ペーパー」が config とずれないこと。"""

    def test_buyフラグがconfigと一致する(self, js):
        from src.utils.helpers import load_config
        jp2db = {"複勝": "fukusho", "単勝": "tansho", "拡連複": "kakurenfuku",
                 "2連複": "nirenfuku", "3連複": "sanrenfuku", "3連単": "sanrentan"}
        cfg = load_config()["betting"]
        want = {jp2db.get(t, t) for t in cfg["bet_types"]}

        blk = re.search(r"const BET_TIER = \{(.+?)\n\};", js, re.S)
        got = {bt for bt, flag in
               re.findall(r"(\w+):\s*\{[^}]*buy:\s*(true|false)", blk.group(1))
               if flag == "true"}
        assert got == want, f"app.js の buy={got} / config の bet_types={want}"
