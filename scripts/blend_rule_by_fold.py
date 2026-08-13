"""見つけた混合ルールが、期間を割っても持つかを見る。

「混合p>=0.10 かつ EV>=1.2」は 5/1〜8/12 の通しで ROI 200%
[133%, 271%] だった。ただし 8 ルール試した中の 1 つで、閾値も同じ
データを見て決めている。今日すでに同じ形で2回外している
（オッズが粗い区分 / 直前情報）ので、期間で割って再現するか確かめる。

一部の期間だけで出るなら、まぐれ当たりが混じっているだけ。
全期間で同じ向きに出るなら、実在する可能性が上がる。

使い方:
    python scripts/blend_rule_by_fold.py <ranker> <from> <to>
        [--w 0.3] [--p 0.10] [--ev 1.2] [--folds 4] [--boot 2000]
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.blend_folds import load            # noqa: E402
from scripts.blend_bet_rules import bootstrap_roi   # noqa: E402


def arg(name: str, default: float) -> float:
    return float(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main() -> None:
    ranker, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    w, pmin, evmin = arg("--w", 0.3), arg("--p", 0.10), arg("--ev", 1.2)
    folds, n_boot = int(arg("--folds", 4)), int(arg("--boot", 2000))
    rng = np.random.default_rng(0)

    R = load(ranker, d1, d2)
    R["pb"] = w * R["pm"] + (1 - w) * R["pk"]
    R["ev"] = R["pb"] * R["odds"]
    sel_all = R[(R["pb"] >= pmin) & (R["ev"] >= evmin)]

    print(f"=== 混合(モデル{w * 100:.0f}%) p>={pmin} かつ EV>={evmin} ===")
    print(f"{d1}〜{d2}  {R['race_id'].nunique():,}レース\n")
    print(f"{'期間':24}{'本数':>7}{'的中率':>8}{'回収率':>8}{'95%区間':>18}"
          f"{'最大1本の寄与':>13}")

    dates = sorted(R["date"].unique())
    edges = [dates[int(len(dates) * i / folds)] for i in range(folds)] + [None]
    groups = []
    for i in range(folds):
        lo, hi = edges[i], edges[i + 1]
        sub = sel_all[(sel_all["date"] >= lo) & ((sel_all["date"] < hi) if hi else True)]
        groups.append((f"{lo}〜", sub))
    groups.append(("全期間", sel_all))

    for label, sel in groups:
        n = len(sel)
        if n < 15:
            print(f"{label:24}{n:>7,}  （少なすぎて判断不能）")
            continue
        pays = (sel["y"] * sel["odds"]).values
        hr = sel["y"].mean()
        roi = pays.sum() / n * 100
        share = pays.max() / pays.sum() * 100 if pays.sum() > 0 else 0
        lo_, hi_, _ = bootstrap_roi(sel, n_boot, rng)
        if label == "全期間":
            print("─" * 78)
        print(f"{label:24}{n:>7,}{hr * 100:>7.1f}%{roi:>7.0f}%"
              f"{f'[{lo_:.0f}%, {hi_:.0f}%]':>18}{share:>12.0f}%")

    print("\n※ 一部の期間でしか出ないなら採用しない。今日すでに2件、")
    print("   片方の窓でだけ出た候補が消えている。")


if __name__ == "__main__":
    main()
