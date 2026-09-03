"""実運用にする賭式の設定を見張る。

2026-09-03 の変更
-----------------
固い4賭式（複勝・単勝・拡連複・2連複）を実運用にした。3連複・3連単は
モデルが確立するまでペーパー。

⚠️⚠️ **最大の罠: 大域ガードのまま賭式を足すと、買い目がほぼ0本になる。**
`min_odds: 1.5` と `min_expected_value: 1.2` が全賭式に効いている
（main.py の refresh_odds 内、_buy の判定）。複勝のオッズ中央値は
**1.00倍**（元返し）、単勝は1.10倍なので、そのまま足すとこうなる:

    賭式      確率で通過   +大域min_odds1.5   +大域min_ev1.2
    複勝          33            1                 1
    単勝          35            4                 3
    拡連複        45            5                 3

エラーは出ない。**「実運用にしたのに買い目が出ない」形で静かに失敗する。**
だから賭式ごとの override を必須にして、ここで見張る。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.helpers import load_config  # noqa: E402

JP2DB = {"複勝": "fukusho", "単勝": "tansho", "拡連複": "kakurenfuku",
         "2連複": "nirenfuku", "3連複": "sanrenfuku", "3連単": "sanrentan"}


@pytest.fixture(scope="module")
def betting():
    return load_config()["betting"]


class TestBuyTypes:
    def test_固い4賭式が実運用になっている(self, betting):
        got = {JP2DB.get(t, t) for t in betting["bet_types"]}
        assert got == {"fukusho", "tansho", "kakurenfuku", "nirenfuku"}, got

    def test_3連複と3連単はペーパーのまま(self, betting):
        """モデルが確立するまで賭け金を付けない（利用者の判断）。"""
        paper = {JP2DB.get(t, t) for t in betting["paper_bet_types"]}
        assert paper == {"sanrenfuku", "sanrentan"}, paper

    def test_買う賭式とペーパーが重ならない(self, betting):
        buy = {JP2DB.get(t, t) for t in betting["bet_types"]}
        paper = {JP2DB.get(t, t) for t in betting["paper_bet_types"]}
        assert not (buy & paper), f"両方に入っている: {buy & paper}"

    def test_6賭式すべてがどちらかに入っている(self, betting):
        both = ({JP2DB.get(t, t) for t in betting["bet_types"]}
                | {JP2DB.get(t, t) for t in betting["paper_bet_types"]})
        assert both == set(JP2DB.values()), f"抜けている: {set(JP2DB.values()) - both}"


class TestOverridesRequired:
    """⚠️ 買う賭式には override が必須。無いと大域ガードで消える。"""

    def test_買う賭式すべてに閾値が明示されている(self, betting):
        ov = betting.get("bet_type_overrides", {})
        for t in betting["bet_types"]:
            bt = JP2DB.get(t, t)
            assert bt in ov, f"{t}({bt}) に override が無い（大域ガードで消える）"
            for k in ("min_odds", "max_odds", "min_ev", "min_model_prob"):
                assert k in ov[bt], f"{t} の {k} が無い"

    def test_固い賭式のmin_oddsが大域より低い(self, betting):
        """複勝のオッズ中央値は1.00倍。大域の1.5だと1本しか残らない。"""
        ov = betting["bet_type_overrides"]
        for bt in ("fukusho", "tansho", "kakurenfuku"):
            assert ov[bt]["min_odds"] <= 1.0, (
                f"{bt} の min_odds={ov[bt]['min_odds']} は高すぎる。"
                f"複勝は元返し(1.0倍)が多く、ほぼ全部落ちる")

    def test_固い賭式はEVで絞っていない(self, betting):
        """EVで絞ると悪化する（3通りの測り方で一致）。EVは表示するが絞りに使わない。"""
        ov = betting["bet_type_overrides"]
        for bt in ("fukusho", "tansho", "kakurenfuku"):
            assert ov[bt]["min_ev"] <= 0.0, (
                f"{bt} が EV で絞っている（min_ev={ov[bt]['min_ev']}）。"
                f"EV>=2.0 の実測回収率は 54.1% で、絞るほど下がる")

    def test_2連複の閾値が動いていない(self, betting):
        """⚠️⚠️ scripts/sept_trial.py の事前登録を守る。

        あれは「R1=現行ルール(確率>=0.30 & EV>=1.2) vs R2=確率>=0.387・EVなし」を
        9月データで**1回だけ**検定する。9月が始まる前にコミット済み。
        いま2連複をR2側に変えると、**唯一汚染されていない検定を実施前に潰す**。
        10月の判定が終わるまでこの値は動かさないこと。
        """
        ov = betting["bet_type_overrides"]["nirenfuku"]
        assert ov["min_model_prob"] == 0.30, (
            f"2連複の確率閾値が {ov['min_model_prob']} に変わっている。"
            f"sept_trial.py の R1 が本番ルールでなくなる")
        assert ov["min_ev"] == 1.2, (
            f"2連複の EV 閾値が {ov['min_ev']} に変わっている（同上）")


class TestThresholdsMatchMeasuredBands:
    """閾値は未使用データ(2〜4月)の帯の切れ目そのもの。勝手に動かさない。

    ⚠️ いま新しく閾値を決めると、両窓のROIを見た後なので必ず甘く出る。
    app.js の CONFIDENCE が唯一の出どころ。
    """

    def _confidence_from_js(self) -> dict:
        js = (ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8")
        blk = re.search(r"const CONFIDENCE = \{(.+?)\n\};", js, re.S)
        assert blk, "app.js の CONFIDENCE を読めない"
        out = {}
        for bt, cuts in re.findall(
                r"(\w+):\s*\{\s*cuts:\s*\[([^\]]+)\]", blk.group(1)):
            out[bt] = [float(x) for x in cuts.split(",")]
        return out

    def test_閾値が画面の最上位帯と一致する(self, betting):
        conf = self._confidence_from_js()
        ov = betting["bet_type_overrides"]
        for bt in ("fukusho", "tansho", "kakurenfuku"):
            want = conf[bt][-1]
            assert ov[bt]["min_model_prob"] == pytest.approx(want), (
                f"{bt}: config {ov[bt]['min_model_prob']} / "
                f"app.js の最上位帯 {want}。画面と買い方がずれている")


class TestRealDataProducesBets:
    """⚠️ 実データで各賭式が0本にならないこと。min_odds の罠の見張り。"""

    def _latest_bets(self):
        for i in range(0, 10):
            d = (date.today() - timedelta(days=i)).isoformat()
            p = ROOT / "docs" / "data" / f"bets_{d}.json"
            if p.exists():
                rows = json.loads(p.read_text(encoding="utf-8"))
                if rows:
                    return d, rows
        return None, None

    def test_直近の実データで買う賭式が0本にならない(self, betting):
        """記録されている確率と板に、いまの閾値を当てて本数を数える。

        ⚠️ bets JSON はまだ古い設定で作られていることがあるので、
        `recommended_amount` ではなく**閾値を当て直して**数える。
        """
        d, rows = self._latest_bets()
        if not rows:
            pytest.skip("bets JSON が無い")
        ov = betting["bet_type_overrides"]
        zero = []
        for t in betting["bet_types"]:
            bt = JP2DB.get(t, t)
            o = ov[bt]
            n = sum(
                1 for b in rows
                if b.get("bet_type") == bt
                and b.get("rule") in ("record", "r5")
                and (b.get("model_prob") or 0) >= o["min_model_prob"]
                and o["min_odds"] <= (b.get("odds") or 0) <= o["max_odds"]
                and (b.get("expected_value") or 0) >= o["min_ev"]
            )
            if n == 0:
                zero.append(f"{t}({bt})")
        assert not zero, (
            f"{d}: 買い目が0本になる賭式 {zero}。"
            f"閾値か min_odds/min_ev が実データと噛み合っていない")

    def test_大域ガードのままなら落ちることを確認する(self, betting):
        """このテスト自身が本物のバグを捕まえられるかの自己点検。

        override を無視して大域の min_odds/min_ev を当てると、
        固い賭式は 1〜5本 に落ち込む。そうならないなら、この検査は
        何も見張っていないので設計を見直すこと。
        """
        d, rows = self._latest_bets()
        if not rows:
            pytest.skip("bets JSON が無い")
        g_odds, g_ev = betting["min_odds"], betting["min_expected_value"]
        ov = betting["bet_type_overrides"]
        starved = []
        for bt in ("fukusho", "tansho", "kakurenfuku"):
            cut = ov[bt]["min_model_prob"]
            n = sum(1 for b in rows
                    if b.get("bet_type") == bt
                    and b.get("rule") in ("record", "r5")
                    and (b.get("model_prob") or 0) >= cut
                    and (b.get("odds") or 0) >= g_odds
                    and (b.get("expected_value") or 0) >= g_ev)
            if n <= 5:
                starved.append(bt)
        assert len(starved) >= 2, (
            "大域ガードを当てても本数が減らない。min_odds の罠が再現しないので、"
            "この検査は何も見張っていない")
