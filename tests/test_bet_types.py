"""6賭式に広げたときの取り決めを固定する。

2026-08-30 に 2連複だけから6賭式（単勝・複勝・拡連複・2連複・3連複・3連単）へ
広げた。そのとき見つかった不具合と、二度と起こしたくない取り決め:

  1. **賭式名の対応表が2箇所にあり、しかも単勝・拡連複・複勝が抜けていた。**
     config に書いても素通りして無視される状態だった。
     → src/models/plackett_luce.BET_TYPE_JP に集約

  2. **記録だけの賭式に賭け金500円が付いていた。** 画面上は「買え」に見える。
     refresh_odds が全候補に fixed_amount を付ける作りのままだった。
     → 買うのは config の bet_types にある賭式で、条件を満たした1点だけ

  3. **1レース上限5本のまま6賭式を通すと賭式が丸ごと落ちる。**
     EV順で上位5本を取る作りだったので、1つの賭式で埋まりうる。
     → 賭式ごとに「確率が最大の1点」を選ぶ（測定と同じ選び方）

画面に出す実測回収率は「確率が最大の1点」で測った値なので、
**選び方が測定と一致していること**が数字の意味を支えている。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.models.plackett_luce import (BET_TYPE_JP, all_bet_probs,  # noqa: E402
                                      to_exp_scores)
from src.utils.helpers import load_config  # noqa: E402

SCORES = {1: 1.2, 2: 0.4, 3: 0.1, 4: -0.3, 5: -0.8, 6: -1.1}
# 賭式 -> (通り数, 確率の合計)
SHAPE = {"tansho": (6, 1.0), "fukusho": (6, 2.0), "kakurenfuku": (15, 3.0),
         "nirentan": (30, 1.0), "nirenfuku": (15, 1.0),
         "sanrenfuku": (20, 1.0), "sanrentan": (120, 1.0)}


# ── 確率そのもの ────────────────────────────────

@pytest.mark.parametrize("bt,shape", SHAPE.items())
def test_確率の通り数と合計(bt, shape):
    """複勝は2着以内なので合計2.0、拡連複は当たりが3組なので3.0になる。"""
    ncomb, total = shape
    rows = all_bet_probs(SCORES)[bt]
    assert len(rows) == ncomb, f"{bt} の通り数が違う"
    assert abs(sum(r["model_prob"] for r in rows) - total) < 1e-6, \
        f"{bt} の確率の合計が {total} にならない"


def test_確率は0から1に収まる():
    for bt, rows in all_bet_probs(SCORES).items():
        for r in rows:
            assert 0.0 <= r["model_prob"] <= 1.0, f"{bt} {r['combination']}"


def test_1号艇が強いときは1号艇絡みが上位():
    """入力の順位が確率に反映されているか（符号の取り違え検出）。"""
    p = all_bet_probs(SCORES)
    assert max(p["tansho"], key=lambda x: x["model_prob"])["combination"] == "1"
    assert max(p["fukusho"], key=lambda x: x["model_prob"])["combination"] == "1"
    assert max(p["kakurenfuku"], key=lambda x: x["model_prob"])["combination"] == "1-2"


# ── 設定と対応表 ────────────────────────────────

def test_設定の賭式がすべて対応表にある():
    """⚠️ 抜けていると config に書いても素通りして無視される。"""
    cfg = load_config()["betting"]
    for t in cfg.get("bet_types", []) + cfg.get("paper_bet_types", []):
        assert t in BET_TYPE_JP, f"{t} が BET_TYPE_JP に無い（設定が無視される）"


def test_買う賭式と記録だけの賭式が重ならない():
    cfg = load_config()["betting"]
    buy = {BET_TYPE_JP[t] for t in cfg.get("bet_types", [])}
    rec = {BET_TYPE_JP[t] for t in cfg.get("paper_bet_types", [])}
    assert not (buy & rec), f"重複: {buy & rec}"


def test_対応表が1箇所にまとまっている():
    """main.py と ev_calculator に別々の表を作り直していないこと。"""
    for path in ("main.py", "src/betting/ev_calculator.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert not re.search(r'\{\s*"単勝"\s*:\s*"tansho"', src), \
            f"{path} が独自の対応表を持っている（BET_TYPE_JP を使うこと）"


def test_確率の賭式が対応表を網羅している():
    assert set(all_bet_probs(SCORES)) == set(BET_TYPE_JP.values())


# ── 賭け金の付け方（実害が出た不具合） ────────────────

def test_賭け金は買う賭式だけ():
    """⚠️ 記録だけの賭式に金額が付くと、画面上「買え」に見える。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("for b in race_bets:")
    block = src[i:i + 700]
    assert "_buy" in block, "買うかどうかの判定なしに金額を付けている"
    assert "fixed_amount if" in block, "全件に fixed_amount を付けている"


def test_賭式ごとに1点だけ選ぶ():
    """EV順の上限本数で切ると、6賭式では賭式が丸ごと落ちる。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "best_of_type" in src, "賭式ごとの1点を選んでいない"
    assert "candidates[:max_bets]" not in src, \
        "EV順で上位N本に切っている（賭式が落ちる）"


def test_画面の賭式ラベルが全賭式そろっている():
    js = (ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"function betTypeLabel[\s\S]{0,400}?\}", js)
    assert m, "betTypeLabel が無い"
    for db_name in BET_TYPE_JP.values():
        assert db_name in m.group(0), f"{db_name} のラベルが無い（英語のまま出る）"
