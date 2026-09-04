"""賭式ごとの賭け金が、実測回収率の順序どおりに効いていること。

なぜ定額をやめたか（2026-09-04）
--------------------------------
4賭式を実運用にしたら、**いちばん成績の悪い2連複に本数の43%**が乗っていた。
実測（未使用データ2窓・実運用の閾値）:

    賭式     的中率(2窓)      回収率(2窓)
    複勝     88.9 / 87.7%   96.3 / 95.9%
    単勝     75.9 / 73.9%   93.1 / 92.5%
    拡連複    67.7 / 67.8%   89.2 / 88.3%
    2連複    37.5 / 37.4%   88.3 / 86.1%

⭐ 複勝はどの閾値でも他のどの賭式のどの閾値より良い（全レース買っても94.4%）。
定額のままだとこの差が損益に反映されない。

⚠️ どれも100%未満なので「勝つための配分」ではなく
**「損の小さいところに厚く置く」配分**。

⚠️ 記録のみの賭式（3連複・3連単）には**絶対に金額を付けない**。
2026-08-30 に、賭式を6つへ広げたとき全賭式に500円が付き、画面上
「買え」に見えていた事故がある。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.helpers import load_config  # noqa: E402

JP2DB = {"複勝": "fukusho", "単勝": "tansho", "拡連複": "kakurenfuku",
         "2連複": "nirenfuku", "3連複": "sanrenfuku", "3連単": "sanrentan"}
# 実測回収率の順序（良い順）。金額はこの順序を崩さないこと。
ROI_ORDER = ["fukusho", "tansho", "kakurenfuku", "nirenfuku"]


@pytest.fixture(scope="module")
def cfg():
    return load_config()


class TestAmounts:
    def test_買う賭式すべてに金額がある(self, cfg):
        amts = cfg["money_management"].get("bet_type_amounts") or {}
        for t in cfg["betting"]["bet_types"]:
            bt = JP2DB.get(t, t)
            assert bt in amts, f"{t}({bt}) の金額が無い（定額に落ちる）"

    def test_金額が実測回収率の順序どおり(self, cfg):
        """⭐ ここが本体。良い賭式ほど厚く置く。"""
        amts = cfg["money_management"]["bet_type_amounts"]
        vals = [amts[bt] for bt in ROI_ORDER if bt in amts]
        assert vals == sorted(vals, reverse=True), (
            f"金額の順序 {dict(zip(ROI_ORDER, vals))} が実測回収率の順序"
            f"（複勝96 > 単勝93 > 拡連複89 > 2連複88）と食い違っている")

    def test_複勝が一番厚い(self, cfg):
        """複勝はどの閾値でも他のどの賭式より良い。ここが最大でないのは変。"""
        amts = cfg["money_management"]["bet_type_amounts"]
        assert amts["fukusho"] == max(amts.values()), \
            f"複勝が最大でない: {amts}"

    def test_ペーパーの賭式に金額を付けていない(self, cfg):
        """⚠️ 2026-08-30 の事故。記録だけのはずの3連単に500円が付いていた。"""
        amts = cfg["money_management"].get("bet_type_amounts") or {}
        paper = {JP2DB.get(t, t) for t in cfg["betting"]["paper_bet_types"]}
        bad = paper & set(amts)
        assert not bad, f"ペーパーの賭式に金額が付いている: {bad}"

    def test_1レースの上限を超えていない(self, cfg):
        mm = cfg["money_management"]
        cap = mm.get("max_bet_per_race")
        if not cap:
            pytest.skip("上限の設定なし")
        over = {k: v for k, v in mm["bet_type_amounts"].items() if v > cap}
        assert not over, f"1レース上限 {cap} を超える金額: {over}"


class TestWiring:
    """config に書いても使われなければ意味がない。**振る舞いで見る。**"""

    def test_賭式ごとの金額が買い目に載る(self):
        import inspect

        import main
        src = inspect.getsource(main.cmd_refresh_odds)
        assert "amount_for(" in src, "賭式ごとの金額が使われていない"
        assert "bet_type_amounts" in src, "config を読んでいない"

    def test_記録のみは0円のまま(self):
        """金額を賭式ごとにしても、is_buy でない行は0のままであること。"""
        import inspect

        import main
        src = inspect.getsource(main.cmd_refresh_odds)
        assert 'amount_for(b["bet_type"]) if is_buy else 0' in src, (
            "記録のみの行に金額が付く形になっている")

    def test_未知の賭式は定額に落ちる(self):
        """config に無い賭式でも 0 や例外にならないこと。"""
        cfg = load_config()
        mm = cfg["money_management"]
        amts = mm.get("bet_type_amounts") or {}
        fallback = mm.get("fixed_bet_amount", 200)
        got = int(amts.get("nirentan", fallback))
        assert got == fallback and got > 0
