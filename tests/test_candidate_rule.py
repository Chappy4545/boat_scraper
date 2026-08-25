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
    """棄却・差し替え済みのルールの記録が本番の成績に混ざらないこと。

    market_blend 43本 / shrink_adj 19本 が DB に残っている。
    どれか1つでも抜けると、その分が「買った買い目」として集計される。
    """
    for path, name in (("main.py", "CANDIDATE_REASONS"),
                       ("src/export.py", "CANDIDATE_REASONS")):
        rhs = _consts(path, name)
        for reason in ("候補ルール(混合)", "候補ルール(縮み補正)", "候補ルール(価値1点)"):
            assert reason in rhs, f"{path}: {reason} が抜けている"


def test_mainとexportで候補の定義が一致する():
    assert (_consts("main.py", "CANDIDATE_REASONS")
            == _consts("src/export.py", "CANDIDATE_REASONS")), \
        "main.py と src/export.py で候補ルールの見送り理由が食い違っている"


def test_daily_checkは現行ルールだけを数える():
    """棄却済みルールの成績と混ぜない（合算すると新しい候補の数字が汚れる）。"""
    rhs = _consts("scripts/daily_check.py", "CANDIDATE_REASON")
    assert "価値1点" in rhs
    assert "混合" not in rhs and "縮み補正" not in rhs


def test_画面側の除外にも新しいルール名が入っている():
    js = (ROOT / "docs/js/app.js").read_text(encoding="utf-8")
    m = re.search(r"CANDIDATE_RULES\s*=\s*\[(.+?)\]", js, re.S)
    assert m, "app.js に CANDIDATE_RULES が無い"
    for r in ("market_blend", "shrink_adj", "top1_value"):
        assert r in m.group(1), f"app.js に {r} が無い"


def test_configの候補ルールが実装と噛み合っている():
    import yaml
    cfg = yaml.safe_load((ROOT / "configs/config.yaml").read_text(encoding="utf-8"))
    cand = cfg["operation"]["candidate_rule"]
    assert cand["name"] == "top1_value"
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


def test_本番と候補が同じ組を選んでも両方残る():
    """DBとJSONを突き合わせるキーにルール名が入っていること。

    候補ルールは本番と同条件で EV だけ補正後オッズで計算するので、同じ
    組合せを選ぶのが普通。キーが (レース,賭式,組) だけだと後勝ちで片方が
    消える。2026-08-24 に実データで51本中15本が該当し、そのまま夜の判定を
    迎えていたら実際に買った11本が候補(賭け金0)に化けていた。
    """
    import main

    r5 = main.bet_key(100, "nirenfuku", "1-3", "r5")
    cand = main.bet_key(100, "nirenfuku", "1-3", "shrink_adj")
    assert r5 != cand, "同じ組合せで本番と候補が同じキーになっている"
    # rule が無い JSON 行（古い形式）は本番扱いにそろえる
    assert main.bet_key(100, "nirenfuku", "1-3", None) == r5


def test_見送り理由とルール名が往復する():
    """DB側(pass_reason)とJSON側(rule)の対応が崩れていないこと。"""
    import main

    assert set(main._DB_REASON_TO_RULE) == set(main.CANDIDATE_REASONS)
    assert set(main._RULE_TO_DB_REASON) == set(main.CANDIDATE_RULES)
    for reason, rule in main._DB_REASON_TO_RULE.items():
        assert main._RULE_TO_DB_REASON[rule] == reason


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


# ── top1_value の本体（src/betting/candidate_rule.py）────────────────

def test_選ぶのは確率が最大の1点でオッズを見ない():
    """過去2件の候補はEV（＝オッズ）で選んで崩れた。ここが最大の違い。"""
    from src.betting.candidate_rule import pick_top1

    combos = [
        {"combination": "1-2", "model_prob": 0.40, "odds": 2.0},
        {"combination": "3-4", "model_prob": 0.10, "odds": 50.0},   # EVは最大
        {"combination": "1-3", "model_prob": 0.25, "odds": 6.0},
    ]
    assert pick_top1(combos)["combination"] == "1-2"


def test_補正後オッズは板より低く出る():
    from src.betting.candidate_rule import adjusted_odds

    a, b, c = 0.2910, 0.5527, 0.2899
    for board in (6.0, 10.0, 20.0, 40.0):
        assert adjusted_odds(board, 0.33, a, b, c) < board


def test_閾値を満たさない1点は記録しない():
    from src.betting.candidate_rule import evaluate

    cfg = {"coef": {"a": 0.2910, "b": 0.5527, "c": 0.2899}, "min_ev": 1.484}
    # 本命すぎてオッズが低い → 見込みEVが閾値に届かない
    low = [{"combination": "1-2", "model_prob": 0.50, "odds": 1.8}]
    assert evaluate(low, cfg) is None
    # オッズが高い1点 → 記録される
    hi = [{"combination": "1-4", "model_prob": 0.33, "odds": 9.0}]
    got = evaluate(hi, cfg)
    assert got is not None
    assert got["expected_value"] >= 1.484


def test_edgeとEVの閾値が同じ意味になっている():
    """min_ev 1.484 は edge 2.0（= 2.0 * 0.742）と同値。config と実装のズレ検知。"""
    import yaml
    from src.betting.candidate_rule import TAKEOUT_KEEP

    cfg = yaml.safe_load((ROOT / "configs/config.yaml").read_text(encoding="utf-8"))
    cand = cfg["operation"]["candidate_rule"]
    assert abs(cand["min_ev"] - cand["min_edge"] * TAKEOUT_KEEP) < 1e-6


def test_不正な入力で落ちない():
    from src.betting.candidate_rule import evaluate

    cfg = {"coef": {"a": 0.2910, "b": 0.5527, "c": 0.2899}, "min_ev": 1.484}
    assert evaluate([], cfg) is None
    assert evaluate([{"combination": "1-2", "model_prob": 0, "odds": 5}], cfg) is None
    assert evaluate([{"combination": "1-2", "model_prob": 0.3, "odds": 0}], cfg) is None
