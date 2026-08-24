"""候補ルールの目印が3ファイルでズレていないかを見る。

見送り理由の文字列（"候補ルール(...)"）は main.py / src/export.py /
scripts/daily_check.py の3箇所に別々に書いてある。import すると循環するので
共有できず、片方だけ直すと静かに壊れる:

  - export 側が古いままだと、候補の行が bets JSON から丸ごと落ちる
    （2026-08-22〜23 に実際に発生。44本→29本、候補11本→0本）
  - 集計側が新しい理由を知らないと、賭けていない買い目が本番ルールの
    成績に混ざる（2026-08-24 に watchdog で発生。93.0%→94.9%）

棄却した market_blend の43本が DB に残っているので、
**過去の理由も引き続き「候補」と判定できること**まで確かめる。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _consts(path: str, name: str) -> str:
    """ソースから定数の右辺をそのまま取り出す（import せずに読む）。"""
    src = (ROOT / path).read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*=\s*(.+)$", src, re.M)
    assert m, f"{path} に {name} が見つからない"
    return m.group(1)


def test_過去の見送り理由も候補として扱われる():
    """market_blend の43本が本番ルールの成績に混ざらないこと。"""
    for path, name in (("main.py", "CANDIDATE_REASONS"),
                       ("src/export.py", "CANDIDATE_REASONS")):
        rhs = _consts(path, name)
        assert "候補ルール(混合)" in rhs, f"{path}: 棄却済みの理由が抜けている"
        assert "候補ルール(縮み補正)" in rhs, f"{path}: 現行の理由が抜けている"


def test_mainとexportで候補の定義が一致する():
    assert (_consts("main.py", "CANDIDATE_REASONS")
            == _consts("src/export.py", "CANDIDATE_REASONS")), \
        "main.py と src/export.py で候補ルールの見送り理由が食い違っている"


def test_daily_checkは現行ルールだけを数える():
    """棄却済みルールの成績と混ぜない（合算すると新しい候補の数字が汚れる）。"""
    rhs = _consts("scripts/daily_check.py", "CANDIDATE_REASON")
    assert "縮み補正" in rhs
    assert "混合" not in rhs


def test_画面側の除外にも新しいルール名が入っている():
    js = (ROOT / "docs/js/app.js").read_text(encoding="utf-8")
    m = re.search(r"CANDIDATE_RULES\s*=\s*\[(.+?)\]", js, re.S)
    assert m, "app.js に CANDIDATE_RULES が無い"
    assert "market_blend" in m.group(1)
    assert "shrink_adj" in m.group(1)


def test_configの候補ルールが実装と噛み合っている():
    import yaml
    cfg = yaml.safe_load((ROOT / "configs/config.yaml").read_text(encoding="utf-8"))
    cand = cfg["operation"]["candidate_rule"]
    assert cand["name"] == "shrink_adj"
    coef = cand["coef"]
    for k in ("a", "b", "c"):
        assert isinstance(coef[k], (int, float)), f"coef.{k} が数値でない"
    # b<1 は「板が高いほど大きく割り引く」、c>0 は「本命ほど安く着地する」。
    # 符号が逆だと補正が縮みを増幅する側に働くので、ここで止める。
    assert 0 < coef["b"] < 1, "b は 0〜1 の間でなければ縮みを表さない"
    assert coef["c"] > 0, "c が正でないとモデル確率が効かない"


def test_補正後オッズは板より小さくなる():
    """このルールの目的そのもの。高いオッズほど強く割り引かれること。"""
    import yaml
    cfg = yaml.safe_load((ROOT / "configs/config.yaml").read_text(encoding="utf-8"))
    c = cfg["operation"]["candidate_rule"]["coef"]

    def adj(o, p):
        return math.exp(c["a"] + c["b"] * math.log(o) + c["c"] * math.log(1 / p))

    p = 0.35
    ratios = [adj(o, p) / o for o in (5, 10, 20, 40)]
    assert all(r < 1 for r in ratios), f"割り引かれていない: {ratios}"
    assert ratios == sorted(ratios, reverse=True), \
        f"オッズが高いほど強く割り引かれていない: {ratios}"


@pytest.mark.parametrize("board,prob", [(8.0, 0.35), (10.0, 0.40), (25.0, 0.31)])
def test_高いオッズはEVが下がる(board, prob):
    """狙い撃ちを止めるのがこのルールの目的。

    ⚠️ 補正は「全部を下げる」のではない。平均への回帰なので、高いオッズは
    下がり、低いオッズは上がる。境目は p=0.35 でおよそ 3.8倍。
    実測でも 確定/板 の中央値は 全通り1.079 / 買った分0.746 で、
    落ちるのは選んだ側だけだった。だから低オッズで下がることを期待して
    テストを書くと、実装ではなくテストのほうが間違う（実際やった）。
    """
    import yaml
    cfg = yaml.safe_load((ROOT / "configs/config.yaml").read_text(encoding="utf-8"))
    c = cfg["operation"]["candidate_rule"]["coef"]
    adj = math.exp(c["a"] + c["b"] * math.log(board) + c["c"] * math.log(1 / prob))
    assert prob * adj < prob * board, "高オッズなのに補正後のEVが下がっていない"


def test_補正が板と一致する境目は現実的な範囲にある():
    """境目が低すぎ/高すぎなら係数の符号か桁を間違えている。"""
    import yaml
    cfg = yaml.safe_load((ROOT / "configs/config.yaml").read_text(encoding="utf-8"))
    c = cfg["operation"]["candidate_rule"]["coef"]
    p = 0.35
    crossover = math.exp((c["a"] + c["c"] * math.log(1 / p)) / (1 - c["b"]))
    assert 2.0 < crossover < 8.0, f"境目が現実的でない: {crossover:.2f}倍"
