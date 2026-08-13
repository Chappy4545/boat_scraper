"""市場×モデルの混合で賭けたときの回収率を、誤差つきで出す。

混合の精度が市場を上回ることは分かった（4期間すべてで +0.4〜1.2%）。
残る問いは「それで賭けたら勝てるのか」。

回収率の誤差は当たりの配当に支配されるため、正規近似では足りない。
ここでは**レース単位のブートストラップ**で信頼区間を出す。同じレースの
買い目は結果が連動する（1つ当たれば他は外れる）ので、行ではなく
レースごと再抽出しないと誤差を小さく見積もってしまう。

的中率の高い形も見る。混合確率に下限を置くと本命寄りになり、本数は
減るが当たりの偏りが小さくなる。

使い方:
    python scripts/blend_bet_rules.py <ranker> <from> <to> [--w 0.3] [--boot 2000]
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.blend_folds import load   # noqa: E402


def bootstrap_roi(sel: pd.DataFrame, n_boot: int, rng) -> tuple[float, float, float]:
    """レース単位で再抽出して回収率の 95% 区間を返す。"""
    races = sel["race_id"].unique()
    by_race = {r: g for r, g in sel.groupby("race_id")}
    out = np.empty(n_boot)
    for i in range(n_boot):
        pick = rng.choice(races, size=len(races), replace=True)
        pay = n = 0.0
        for r in pick:
            g = by_race[r]
            pay += float((g["y"] * g["odds"]).sum())
            n += len(g)
        out[i] = pay / n * 100 if n else 0.0
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5)), float(out.std())


def main() -> None:
    ranker, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    w = float(sys.argv[sys.argv.index("--w") + 1]) if "--w" in sys.argv else 0.3
    n_boot = int(sys.argv[sys.argv.index("--boot") + 1]) if "--boot" in sys.argv else 2000
    rng = np.random.default_rng(0)

    R = load(ranker, d1, d2)
    R["pb"] = w * R["pm"] + (1 - w) * R["pk"]
    R["ev"] = R["pb"] * R["odds"]
    R["ratio"] = R["pb"] / R["pk"].clip(lower=1e-9)

    print(f"=== {d1}〜{d2}  {R['race_id'].nunique():,}レース / {len(R):,}組合せ "
          f"（混合 モデル{w * 100:.0f}%）===")
    print(f"レース単位ブートストラップ {n_boot:,} 回\n")
    print(f"{'ルール':30}{'本数':>7}{'的中率':>8}{'回収率':>8}"
          f"{'95%区間':>18}{'平均配当':>9}")

    rules = [
        ("混合EV >= 1.2", R["ev"] >= 1.2),
        ("混合EV >= 1.5", R["ev"] >= 1.5),
        ("混合EV >= 2.0", R["ev"] >= 2.0),
        ("混合p>=0.10 & EV>=1.2", (R["pb"] >= 0.10) & (R["ev"] >= 1.2)),
        ("混合p>=0.15 & EV>=1.2", (R["pb"] >= 0.15) & (R["ev"] >= 1.2)),
        ("混合p>=0.20 & EV>=1.2", (R["pb"] >= 0.20) & (R["ev"] >= 1.2)),
        ("混合p>=0.25 & EV>=1.1", (R["pb"] >= 0.25) & (R["ev"] >= 1.1)),
        ("[参考] モデルEV>=1.2 p>=0.30",
         (R["pm"] >= 0.30) & (R["pm"] * R["odds"] >= 1.2)),
    ]
    for label, mask in rules:
        sel = R[mask]
        n = len(sel)
        if n < 50:
            print(f"{label:30}{n:>7,}  （少なすぎて判断不能）")
            continue
        hr = sel["y"].mean()
        pay = (sel["y"] * sel["odds"]).sum()
        roi = pay / n * 100
        avg = pay / max(sel["y"].sum(), 1)
        lo, hi, _ = bootstrap_roi(sel, n_boot, rng)
        flag = "  ← 100%超" if lo > 100 else ""
        print(f"{label:30}{n:>7,}{hr * 100:>7.1f}%{roi:>7.0f}%"
              f"{f'[{lo:.0f}%, {hi:.0f}%]':>18}{avg:>8.1f}倍{flag}")

    print("\n※ 95%区間が 100% を跨いでいるものは、勝てると言えない。")


if __name__ == "__main__":
    main()
