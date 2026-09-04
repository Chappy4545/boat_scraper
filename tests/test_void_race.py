"""不成立（全額返還）のレースを、勝ちでも負けでもなく扱えること。

2026-09-04 びわこ9R で発覚
--------------------------
フライング多発で**2艇しか完走せず**、7賭式中5つが「不成立」だった:

    3連単 不成立 ¥100   2連複 不成立 ¥100   複勝 不成立 ¥100
    3連複 不成立 ¥100   拡連複 不成立 ¥100
    2連単 4-5  ¥100    単勝  4    ¥100     ← この2つだけ成立

3段階で壊れていた:

1. **払戻テーブルの解析** … 組番セルに数字が無い行（＝不成立）を捨てていた
2. **払戻一覧に載らない** … 不成立のレースは払戻一覧ページに出てこない。
   実測: びわこは11レースしか列挙されず 9R が欠けていた。
   そのため `collect_day_results` は永久に取りに行かなかった
3. **判定** … 当たり組番が無い＝「払戻が引けない」＝**外れ(False)** にされた

利用者からの報告は「買い目確定欄に結果が反映されない買い目がある」。
着順が入る前は未判定のまま残り、着順が入ると誤って**負け**にされる。

⚠️ 正しい扱いは**返還**。的中率にも回収率にも入れない。
   集計は is_hit が None のものを除くので、is_void で表せば数字は自動的に正しい。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scraping.official import VOID_COMBO  # noqa: E402

# 実物と同じ構造（びわこ9R の払戻テーブル）
VOID_HTML = """
<html><body><table>
  <tr class="is-p3-0"><td rowspan="2">3連単</td>
      <td><span class="numberSet1">不成立</span></td><td>&yen;100</td></tr>
  <tr class="is-p3-0"><td rowspan="2">2連単</td>
      <td><span class="numberSet1_number">4</span>
          <span class="numberSet1_number">5</span></td><td>&yen;100</td></tr>
  <tr class="is-p3-0"><td rowspan="2">2連複</td>
      <td><span class="numberSet1">不成立</span></td><td>&yen;100</td></tr>
  <tr class="is-p3-0"><td rowspan="2">単勝</td>
      <td><span class="numberSet1_number">4</span></td><td>&yen;100</td></tr>
</table></body></html>
"""


class TestParser:
    def _sc(self):
        from src.scraping.official import BoatRaceScraper
        from src.utils.helpers import load_config
        return BoatRaceScraper(load_config())

    def test_不成立の行を捨てない(self):
        """⚠️ ここで捨てると、その賭式は永久に判定できない。"""
        df = self._sc()._parse_payouts(VOID_HTML, "11", date(2026, 9, 4), 9)
        got = dict(zip(df.bet_type, df.combination))
        assert got.get("sanrentan") == VOID_COMBO, "3連単の不成立を捨てている"
        assert got.get("nirenfuku") == VOID_COMBO, "2連複の不成立を捨てている"

    def test_成立した賭式は普通に読める(self):
        """不成立の対応で、正常な行を壊していないこと。"""
        df = self._sc()._parse_payouts(VOID_HTML, "11", date(2026, 9, 4), 9)
        got = dict(zip(df.bet_type, df.combination))
        assert got.get("nirentan") == "4-5"
        assert got.get("tansho") == "4"

    def test_数字も不成立も無い行は捨てる(self):
        """空セルまで拾うと、実在しない払戻が入る。"""
        html = """<html><body><table>
          <tr class="is-p3-0"><td rowspan="2">3連単</td>
              <td><span class="numberSet1"></span></td><td>&yen;100</td></tr>
        </table></body></html>"""
        df = self._sc()._parse_payouts(html, "11", date(2026, 9, 4), 9)
        assert df.empty, f"空の組番を拾っている: {df.to_dict('records')}"

    def test_目印は実在しない組番(self):
        """VOID_COMBO が普通の組番と衝突すると、誤って的中扱いになる。"""
        assert not any(ch.isdigit() for ch in VOID_COMBO), VOID_COMBO
        assert "-" not in VOID_COMBO


class TestCollectsUnlistedRaces:
    """⚠️ 不成立のレースは払戻一覧に出てこない。個別に取りに行くこと。"""

    def test_払戻の無いレースを拾う関数がある(self):
        import inspect

        import main
        assert hasattr(main, "_collect_unlisted_races")
        src = inspect.getsource(main._collect_unlisted_races)
        assert "NOT EXISTS" in src and "payouts" in src, \
            "払戻の無いレースを探していない"

    def test_結果収集から呼ばれている(self):
        import inspect

        import main
        src = inspect.getsource(main.cmd_collect_results)
        assert "_collect_unlisted_races" in src, "呼ばれていない（永久に取れない）"

    def test_件数が多すぎるときは見送る(self):
        """丸ごと欠けている日は別の原因。数十件も個別取得するのはおかしい。"""
        import inspect

        import main
        src = inspect.getsource(main._collect_unlisted_races)
        assert "> 40" in src or ">40" in src, "暴走よけが無い"


class TestJudging:
    def test_判定が不成立を返還として扱う(self):
        import inspect

        import main
        src = inspect.getsource(main.cmd_judge)
        assert "VOID_COMBO" in src, "不成立を見ていない"
        assert "is_void" in src, "返還として記録していない"
        # ⚠️ is_hit を埋めてはいけない（的中率・回収率に入ってしまう）
        i = src.index("void_types")
        blk = src[i:i + 900]
        assert "bet.is_void = True" in blk
        assert "continue" in blk, "返還のあとも判定を続けている"

    def test_書き出しが誤判定を打ち消す(self):
        """⚠️ 一度 False にされた行を直せないと、返還が負けのまま残る。"""
        import inspect

        from src import export
        src = inspect.getsource(export.fill_results_into_json)
        assert "void_keys" in src
        i = src.index("void_keys")
        blk = src[i:]
        assert 'b["is_hit"] = None' in blk, \
            "既に入った is_hit を打ち消していない（返還が負けのまま残る）"


class TestDisplay:
    def test_返還は終了扱いにする(self):
        """⚠️ 含めないと「買い目確定」欄に永久に残る（利用者が気づいた症状）。"""
        js = (ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8")
        i = js.index("const settledOf")
        line = js[i:js.index("\n", i)]
        assert "is_void" in line, f"返還が終了扱いになっていない: {line}"

    def test_返還と表示する(self):
        js = (ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8")
        assert "返還" in js, "画面に返還の表示が無い"

    def test_集計には入れない(self):
        """的中率の分母は is_hit が true/false のものだけであること。"""
        js = (ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8")
        for marker in ("const settled  = bets.filter(", "const settled = bets.filter("):
            if marker in js:
                i = js.index(marker)
                line = js[i:js.index("\n", i)]
                assert "is_void" not in line, \
                    f"返還が的中率の分母に入っている: {line}"


class TestRealData:
    def test_9月4日のびわこ9Rが返還になっている(self):
        """実データでの答え合わせ。2艇しか完走しなかったレース。"""
        import json
        p = ROOT / "docs" / "data" / "bets_2026-09-04.json"
        if not p.exists():
            pytest.skip("bets JSON なし")
        rows = [b for b in json.loads(p.read_text(encoding="utf-8"))
                if b.get("stadium_name") == "びわこ" and b.get("race_no") == 9
                and b.get("rule") in ("record", "r5")]
        if not rows:
            pytest.skip("該当レースの買い目なし")
        void = {b["bet_type"] for b in rows if b.get("is_void")}
        assert "nirenfuku" in void, "2連複（賭け金つき）が返還になっていない"
        assert len(void) == 5, f"返還が5賭式のはずが {len(void)}: {void}"
        # 単勝は成立していた（4号艇が1着）ので、外れが正しい
        tansho = [b for b in rows if b["bet_type"] == "tansho"][0]
        assert tansho.get("is_hit") is False and not tansho.get("is_void"), \
            "成立した賭式まで返還にしている"

    def test_未判定のまま残っている買い目が無い(self):
        """⚠️ 利用者の報告そのもの。判定も返還も付かない行が残らないこと。"""
        import glob
        import json
        import os
        bad = []
        for p in sorted(glob.glob(str(ROOT / "docs" / "data" / "bets_2026-09-*.json"))):
            d = os.path.basename(p)[5:15]
            if d >= str(date.today()):        # 今日はまだ終わっていない
                continue
            rows = json.loads(Path(p).read_text(encoding="utf-8"))
            n = sum(1 for b in rows
                    if b.get("rule") in ("record", "r5")
                    and b.get("is_hit") is None and not b.get("is_void"))
            if n:
                bad.append((d, n))
        assert not bad, f"未判定のまま残っている: {bad}"
