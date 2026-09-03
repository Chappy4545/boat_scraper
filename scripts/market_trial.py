"""モデルを市場より賢くできるか。5案を未見データで一度だけ比べる。

⚠️ 事前登録（2026-09-03・**結果を1つも見る前に書いてコミットする**）
==================================================================
仮説・比べ方・判定基準・停止条件をここで固定する。実行して結果を見た後に
この docstring を書き換えない。書き換えたら事前登録の意味が消える。

なぜやり直すのか — 2026-08-12 の不採用は信用できない
----------------------------------------------------
同じ趣旨の実験が `e7b1fcb` で行われ、「市場を入れると悪化する」と結論した:

    A 現行モデル        対数損失 0.18771  ← 最良とされた
    -  市場そのもの              0.19445
    B 市場込みで予測            0.20331
    C 市場の誤りを学習          0.20957

**この比較は成立していない。3つの欠陥がある:**

1. ⚠️⚠️ **A だけが in-sample だった。**
   `scripts/test_market_model.py` は A の確率を `load_ranker()`（本番モデル）
   で作る。当時の本番モデルは実験当日の朝 09:43 に訓練されており
   （`training_summary_20260812_094334`）、検証期間 07-01〜08-11 を
   **訓練データに含む**。一方 B・C は 05-01〜06-30 だけで訓練して
   07-01 以降を予測している。**A だけが答えを見ていた。**
2. 検証が **854レース**しかない（`is_live=1` のオッズがそれだけだった）。
3. その実験自身の基準「現行モデルが市場を上回る(0.18771 < 0.19445)」は
   **翌日 08-13 に否定されている**（未見データで市場が 3.0% ±0.9 優位）。
   基準が誤っていた実験の結論は、そのままでは使えない。

いま市場確率を作れるレースは **18,307**（2連複15通り+着順、5〜9月）で20倍。
→ [[project_backtest_leak]] [[project_calibration_priority]]

比べるもの
----------
すべて**同一のウォークフォワード枠・同一レース集合**で、
**5案とも自前で訓練する**（`load_ranker()` は使わない。上記1の再発防止）。

    A 現行         FEATURE_COLS 34項目のみ                     基準線
    B 市場込み     34 + 市場の含意確率（2連複から艇別に作る）    08-12の再測定
    C 残差学習     市場確率からのズレを学習                      08-12の再測定
    D 直前情報込み 34 + EXTRA_FEATURE_COLS 13項目               既に+0.52%の実績
    E 二段階補正   ⭐**未実施・主判定**

E の作り: 土台モデルは**市場を一度も見ない**（＝Aと同じ特徴量）。
後段で logit(p_model) と logit(p_market) を合成する校正器だけを、
**土台の訓練期間とも検証期間とも重ならない期間**で当てる。
B/C が負けた原因が「モデルが市場に従うことを学習する」なら、
E は土台が市場を見ないので**構造上その負け方をしない**。
既存の固定ブレンド（p=0.3*model+0.7*market）とも別物（あれは学習しない）。
→ [[project_blend_candidate]]

⚠️ A と B/C は訓練できる期間が違う（B/C はオッズのある5月以降）。
**B/C と比べるときは A も同じ部分集合に制限する。**
しないと「市場の効果」と「訓練データ量の差」が混ざる。

市場の含意確率の作り方
----------------------
2連複の板から、賭式内で正規化して控除率を除く:

    P_market({i,j}) = (1/odds_ij) / Σ_kl (1/odds_kl)
    艇ごと:  P(艇i が2着以内) = Σ_{j≠i} P_market({i,j})     6艇の和は 2.00

判定（先に決める）
------------------
    主判定  E の対数損失 < 市場の対数損失
            レース単位ブートストラップの95%区間が0を跨がないこと
            ⚠️ レース単位で取る。同じレースの15通りは連動するので
               行単位のSEは小さく出すぎる
    副判定  D も同じ形で見る。2件検定するので α=0.025 ずつ（Bonferroni）
    参考    A / B / C は表に出すだけ。**判定には使わない**
    併記    ⭐**複勝の最上位帯だけに絞った対数損失も必ず出す。**
            実質控除率が約2%の唯一の区域なので、全体で負けていても
            ここで勝てていれば意味がある（逆もある）
            → [[project_fukusho_floor]]

    ⚠️ 結果を見てから案を足さない。賭式を増やさない。期間をずらさない。
    ⚠️ 回収率で判定しない（誤差±5〜15pt。対数損失は1桁小さい）

停止条件
--------
どれも市場を下回らなければ、**この方向は打ち切る**と明記して報告する。
「惜しい」「もう少しデータがあれば」を理由に条件を足さない。

使い方
------
    python scripts/market_trial.py                 # 既定のウォークフォワード
    python scripts/market_trial.py --quick         # 打ち切り1つだけ（動作確認）
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from src.features.builder import (  # noqa: E402
    EXTRA_FEATURE_COLS, FEATURE_COLS, build_features,
)
from src.ingestion.database import get_engine  # noqa: E402
from src.models import plackett_luce as pl  # noqa: E402

BT = "nirenfuku"
NEED = 15                      # 2連複の全通り
EPS = 1e-9


# ── 市場の含意確率 ────────────────────────────────
def load_market(d1: str, d2: str) -> dict[int, dict[str, float]]:
    """レースごとの {組合せ: 市場確率}。全通り揃ったレースだけ返す。

    ⚠️ 1通りでも欠けると正規化が壊れる（合計が1にならない）ので、
    揃っていないレースは丸ごと捨てる。
    """
    with get_engine().connect() as c:
        rows = c.execute(text(
            "SELECT o.race_id, o.combination, o.odds FROM odds o "
            "JOIN races r ON r.id = o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.bet_type = :bt "
            "AND o.odds > 0"), {"d1": d1, "d2": d2, "bt": BT}).fetchall()
    per: dict[int, dict[str, float]] = defaultdict(dict)
    for rid, cb, o in rows:
        per[int(rid)][str(cb)] = float(o)
    out = {}
    for rid, od in per.items():
        if len(od) != NEED:
            continue
        inv = {cb: 1.0 / o for cb, o in od.items()}
        tot = sum(inv.values())
        if tot <= 0:
            continue
        out[rid] = {cb: v / tot for cb, v in inv.items()}
    return out


def boat_market(mkt_race: dict[str, float]) -> dict[int, float]:
    """組合せの市場確率 → 艇ごとの「2着以内」確率。6艇の和は 2.00。"""
    per = defaultdict(float)
    for cb, p in mkt_race.items():
        a, b = (int(x) for x in cb.split("-"))
        per[a] += p
        per[b] += p
    return dict(per)


# ── 評価 ──────────────────────────────────────────
def logloss_by_race(p: np.ndarray, y: np.ndarray, race: np.ndarray) -> dict:
    """レース単位に平均した対数損失と、レースごとの値（区間推定用）。"""
    p = np.clip(p, EPS, 1 - EPS)
    per_row = -(y * np.log(p) + (1 - y) * np.log(1 - p))
    by = defaultdict(list)
    for r, v in zip(race, per_row):
        by[r].append(v)
    per_race = np.array([np.mean(v) for v in by.values()])
    return {"mean": float(per_race.mean()), "per_race": per_race,
            "races": list(by.keys())}


def boot_diff(a_per_race: np.ndarray, b_per_race: np.ndarray,
              n: int = 4000, seed: int = 0) -> tuple[float, float]:
    """a − b の95%区間。**同じレースを対で**再抽出する。"""
    assert len(a_per_race) == len(b_per_race)
    rng = np.random.default_rng(seed)
    k = len(a_per_race)
    out = np.empty(n)
    for i in range(n):
        idx = rng.integers(0, k, k)
        out[i] = a_per_race[idx].mean() - b_per_race[idx].mean()
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="打ち切り1つだけ")
    ap.parse_args()
    print(__doc__.split("使い方")[0])
    print("※ 実装は次のコミット。この時点では事前登録のみ。")


if __name__ == "__main__":
    main()
