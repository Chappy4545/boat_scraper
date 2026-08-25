"""PL補正を入れたら「勝てる見込み」が動くのかを確かめる。

背景:
  勝つ条件は 実測の的中率 ÷ 市場の見立て > 1/0.742 = 1.348。
  補正なしで測ると最良の区分が 1.145 で 18% 足りなかった
  （scratchpad/edge_scan.py・2026-08-24）。

  PL変換の補正で対数損失は 1〜3pt 改善した。だが「較正が良くなる」ことと
  「勝てる区分ができる」ことは別。統合の労力をかける前にここを見る。

⚠️ 確定オッズで評価している。実際は板で選ぶのでこのまま取れる値ではない。
   上限を測る計算。

使い方:
    python scripts/edge_after_fix.py <model.joblib> \
        <補正の訓練from> <補正の訓練to> <評価from> <評価to>
"""
from __future__ import annotations

import logging
import math
import random
import sys
import warnings
from pathlib import Path

import joblib
import numpy as np

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from learn_pair_transform import featurize, clip           # noqa: E402
from apply_pair_transform import model_races, _cols_for, PROD   # noqa: E402
from src.utils.helpers import load_config                  # noqa: E402

KEEP = 0.742


def build(races, use_corr, corr):
    """レースごとに [(確率, 確定オッズ, 的中)] を返す。"""
    out = []
    for r in races:
        f, keys, plp = featurize(r)
        if use_corr:
            p = plp * np.exp(corr.predict(f))
            p = p / p.sum()
        else:
            p = plp
        # 確定オッズは 市場確率(控除除去済み) から戻す: odds = KEEP / q
        grp = [(float(pv), KEEP / r["pairs"][k]["mkt"], r["pairs"][k]["hit"])
               for k, pv in zip(keys, p)]
        out.append(grp)
    return out


def roi(sel):
    k = sum(len(g) for g in sel)
    if not k:
        return 0.0
    return sum(o * 100 for g in sel for p, o, y in g if y) / (k * 100) * 100


def scan(label, groups):
    print()
    print("=== %s ===" % label)
    print("edge帯        組合せ  的中率  市場の見立て  実測/市場  回収率")
    best = None
    for lo, hi in [(0, .8), (.8, 1.0), (1.0, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, 99)]:
        sel = [[x for x in g if lo <= (x[0] / (KEEP / x[1])) < hi] for g in groups]
        sel = [g for g in sel if g]
        k = sum(len(g) for g in sel)
        if k < 100:
            continue
        hits = sum(1 for g in sel for x in g if x[2])
        q = sum(KEEP / x[1] for g in sel for x in g) / k
        ratio = (hits / k) / q if q else 0
        r = roi(sel)
        # ⚠️ 判定は「比」ではなく**回収率**で行う。比は市場確率の平均で割るので、
        # 大穴が混ざる帯では実際の損益とずれる（2026-08-25 実測: 比 1.524→1.606 と
        # 上がったのに回収率は 113%→87% と下がった）。賭けるのは1点ずつなので
        # 1点あたりの払戻＝回収率が本命。
        if best is None or r > best[0]:
            best = (r, lo, hi, k, ratio)
        print("%.1f〜%-5.1f %8d %7.1f%% %11.1f%% %9.3f %7.1f%%"
              % (lo, hi, k, hits / k * 100, q * 100, ratio, r))
    if best:
        print("  最良 edge %.1f〜%.1f: 回収 %.1f%%（比 %.3f・%d組）"
              % (best[1], best[2], best[0], best[4], best[3]))
    return best


def main():
    import lightgbm as lgb

    mpath, t1, t2, e1, e2 = sys.argv[1:6]
    if Path(mpath).resolve() == Path(PROD).resolve():
        raise SystemExit("本番モデルは in-sample になるので渡さないこと")
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    model = joblib.load(mpath)
    cols = _cols_for(model)

    print("1) 補正を学習（モデルの艇確率→市場の2連複, %s〜%s）" % (t1, t2))
    tr = model_races(model, cols, temp, t1, t2)
    X, Y = [], []
    for r in tr:
        f, keys, plp = featurize(r)
        X.append(f)
        Y.append([math.log(clip(r["pairs"][k]["mkt"]) / clip(p))
                  for k, p in zip(keys, plp)])
    corr = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.05,
                             num_leaves=31, min_child_samples=50, verbose=-1)
    corr.fit(np.vstack(X), np.concatenate(Y))
    print("   %d レース / %d行" % (len(tr), len(np.concatenate(Y))))

    print("2) 評価期間（%s〜%s）" % (e1, e2))
    te = model_races(model, cols, temp, e1, e2)
    print("   %d レース" % len(te))

    b0 = scan("補正なし（現行のPL）", build(te, False, corr))
    b1 = scan("補正あり", build(te, True, corr))

    print()
    if b0 and b1:
        print("最良帯の回収率: %.1f%% → %.1f%%  （%+.1f pt）"
              % (b0[0], b1[0], b1[0] - b0[0]))
        if b1[0] > b0[0] + 3:
            print("→ 選択にも効いた。統合する価値がある")
        elif b1[0] < b0[0] - 3:
            print("→ **悪化**。較正は良くなったが買い目の選び方は悪くなっている")
        else:
            print("→ ほとんど動かない。較正は良くなったが選択には効いていない")
        print()
        print("⚠️ 確定オッズでの上限値。実際は板で選ぶのでこのままは取れない。")
        print("⚠️ 1窓では決めないこと。もう一方の窓でも同じ向きか確認する。")


if __name__ == "__main__":
    main()
