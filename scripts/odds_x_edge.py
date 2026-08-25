"""最後の未検証軸: 「短いオッズ」×「モデルの優位」を掛け合わせる。

これまでの測定:
  - モデルを使わず確定オッズ2〜3倍を全部買うと 84.5%（基準74.2%）
    ＝ 本命が買われ足りない構造的な偏りが +10pt ある
  - edge（モデル÷市場）だけで選ぶと一貫して約79%
  - この2つを**掛け合わせた形**は未検証。全オッズ帯を横断して edge を
    見ていたため、偏りのある領域に絞った上での優位を見ていない

⚠️ セルを25個も切るので、片方の窓でだけ良く見えるものは必ず出る。
   **両窓で100%超**を条件にする。これを先に決めてから実行すること。

使い方:
    python scripts/odds_x_edge.py
"""
from __future__ import annotations

import logging
import random
import sys
import warnings
from pathlib import Path

import joblib

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apply_pair_transform import model_races, _cols_for   # noqa: E402
from src.utils.helpers import load_config                 # noqa: E402

KEEP = 0.742
SP = ("C:/Users/kcs15/AppData/Local/Temp/claude/"
      "c--Users-kcs15-OneDrive--------boat-scraper/"
      "ff2b3698-6995-46c5-ab71-d5feb0d839dc/scratchpad")

# 当日その予測に使われたモデルと、その期間（すべて未見）
WINDOWS = [
    ("窓1 8/13〜21", f"{SP}/model_0812.joblib", "2026-08-13", "2026-08-21"),
    ("窓2 8/22〜23", f"{SP}/model_0821.joblib", "2026-08-22", "2026-08-23"),
]
ODDS_BANDS = [(1, 3), (3, 5), (5, 10), (10, 30), (30, 10 ** 9)]
EDGE_BANDS = [(0, 1.0), (1.0, 1.2), (1.2, 1.5), (1.5, 99)]


def cells(races):
    """(オッズ帯, edge帯) → レースごとの行リスト。"""
    out = {}
    for oi, (olo, ohi) in enumerate(ODDS_BANDS):
        for ei, (elo, ehi) in enumerate(EDGE_BANDS):
            sel = []
            for g in races:
                grp = []
                for p, o, y in g:
                    q = KEEP / o
                    edge = p / q if q else 0
                    if olo <= o < ohi and elo <= edge < ehi:
                        grp.append((p, o, y))
                if grp:
                    sel.append(grp)
            out[(oi, ei)] = sel
    return out


def roi(sel):
    k = sum(len(g) for g in sel)
    if not k:
        return 0.0, 0
    return sum(o * 100 for g in sel for _p, o, y in g if y) / (k * 100) * 100, k


def boot_lo(sel, T=800):
    """レース単位ブートストラップの下側2.5%。"""
    if not sel:
        return 0.0
    random.seed(0)
    v = sorted(roi([random.choice(sel) for _ in sel])[0] for _ in range(T))
    return v[int(.025 * T)]


def flatten(races):
    """model_races の出力を [(p, 確定オッズ, 的中)] のレース束にする。"""
    out = []
    for r in races:
        grp = []
        for k, v in r["pairs"].items():
            grp.append((v["pl"], KEEP / v["mkt"], v["hit"]))
        out.append(grp)
    return out


def main():
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    results = {}
    for label, mpath, d1, d2 in WINDOWS:
        print("読み込み: %s" % label)
        model = joblib.load(mpath)
        races = flatten(model_races(model, _cols_for(model), temp, d1, d2))
        print("  %d レース" % len(races))
        results[label] = cells(races)

    labels = [w[0] for w in WINDOWS]
    print()
    print("=== 回収率（左: %s / 右: %s）===" % tuple(labels))
    hdr = "確定オッズ帯 "
    for elo, ehi in EDGE_BANDS:
        hdr += "  edge %.1f〜%-4.1f " % (elo, ehi)
    print(hdr)
    both = []
    for oi, (olo, ohi) in enumerate(ODDS_BANDS):
        lbl = "%d〜%d倍" % (olo, ohi) if ohi < 10 ** 9 else "%d倍〜" % olo
        line = "%-12s" % lbl
        for ei in range(len(EDGE_BANDS)):
            r1, n1 = roi(results[labels[0]][(oi, ei)])
            r2, n2 = roi(results[labels[1]][(oi, ei)])
            if n1 < 80 or n2 < 40:
                line += "     --/--     "
                continue
            line += " %5.0f%%/%5.0f%% " % (r1, r2)
            if r1 > 100 and r2 > 100:
                both.append((oi, ei, r1, n1, r2, n2))
        print(line)

    print()
    print("=== 両窓で100%%を超えたセル ===")
    if not both:
        print("  なし")
        print("  → 短いオッズに絞ってもモデルの優位は乗らなかった")
    else:
        for oi, ei, r1, n1, r2, n2 in both:
            olo, ohi = ODDS_BANDS[oi]
            elo, ehi = EDGE_BANDS[ei]
            sel1 = results[labels[0]][(oi, ei)]
            sel2 = results[labels[1]][(oi, ei)]
            print("  オッズ %s / edge %.1f〜%.1f" %
                  ("%d〜%d倍" % (olo, ohi) if ohi < 10 ** 9 else "%d倍〜" % olo,
                   elo, ehi))
            print("     窓1 %5.0f%% (%d組, 下側2.5%% %.0f%%) / 窓2 %5.0f%% (%d組, 下側2.5%% %.0f%%)"
                  % (r1, n1, boot_lo(sel1), r2, n2, boot_lo(sel2)))

    print()
    print("=== 参考: オッズ帯だけで全部買う（モデル不使用）===")
    for oi, (olo, ohi) in enumerate(ODDS_BANDS):
        s1 = [g for ei in range(len(EDGE_BANDS)) for g in results[labels[0]][(oi, ei)]]
        s2 = [g for ei in range(len(EDGE_BANDS)) for g in results[labels[1]][(oi, ei)]]
        r1, n1 = roi(s1)
        r2, n2 = roi(s2)
        lbl = "%d〜%d倍" % (olo, ohi) if ohi < 10 ** 9 else "%d倍〜" % olo
        print("  %-10s %5.0f%% (%5d) / %5.0f%% (%5d)" % (lbl, r1, n1, r2, n2))


if __name__ == "__main__":
    main()
