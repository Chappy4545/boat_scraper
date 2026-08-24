"""市場に負けているのは「艇の評価」か「組への変換」か。

memory (project_market_is_ahead) に未追求として残っていた仮説:
  単勝(艇1つ)だと市場との差は小さいのに、2連複(組)だと大きく開く。
  組を作る段階＝Plackett-Luce 変換が精度を落としているのでは。

同じモデル・同じレースで、
  艇単位: PL の1着確率 vs 単勝オッズの市場確率
  組単位: PL の2連複確率 vs 2連複オッズの市場確率
を測れば、どちらで落としているかが分かる。

どちらでもない（両方同じだけ負けている）なら、モデルそのものを
良くするしかない。艇単位が互角なら、変換を直すのが最短。

⚠️ **必ずその期間を見ていないモデルを渡すこと。**
本番モデル(data/processed/models/)は毎日〜毎週その日までのデータで
再訓練されるので、渡すと in-sample になり必ず良く出る。実際 2026-08-24 に
本番モデルで 8/19〜23 を測って「単勝 +11.6%」と出たが、そのモデルは
8/23 に再訓練されており評価期間を訓練に含んでいた。
当日使われたモデルは `git log -- data/processed/models/ranker_lightgbm.joblib`
で辿り、`git cat-file -p <blob>` で取り出せる。

使い方:
    python scripts/where_we_lose.py <model.joblib> 2026-08-19 2026-08-21
"""
from __future__ import annotations

import logging
import math
import random
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.features.builder import (            # noqa: E402
    build_features, FEATURE_COLS, EXTRA_FEATURE_COLS,
)
from src.models import plackett_luce as pl    # noqa: E402
from src.ingestion.database import get_engine  # noqa: E402
from src.utils.helpers import load_config      # noqa: E402

BASE_COLS = [c for c in FEATURE_COLS if c not in EXTRA_FEATURE_COLS]
PROD = "data/processed/models/ranker_lightgbm.joblib"


def _cols_for(model) -> list[str]:
    n = getattr(model, "n_features_", None) or getattr(model, "n_features_in_", None)
    if n is None or n == len(BASE_COLS):
        return BASE_COLS
    return BASE_COLS + EXTRA_FEATURE_COLS


def clip(p: float) -> float:
    return min(max(p, 1e-9), 1 - 1e-9)


def paired(rows):
    """rows: [(p_model, q_market, hit)] → 市場比の差(%)と2SE。正ならモデルの勝ち。"""
    n = len(rows)
    dif, mk = [], 0.0
    for p, q, y in rows:
        lp = -(math.log(clip(p)) if y else math.log(1 - clip(p)))
        lq = -(math.log(clip(q)) if y else math.log(1 - clip(q)))
        dif.append(lq - lp)
        mk += lq
    m = sum(dif) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in dif) / (n - 1)) if n > 1 else 0.0
    base = mk / n
    return m / base * 100, 2 * sd / math.sqrt(n) / base * 100


def boot_by_race(groups, T=1500):
    """レース単位ブートストラップで区間を出す。"""
    def stat(sample):
        flat = [x for g in sample for x in g]
        return paired(flat)[0]
    random.seed(0)
    out = sorted(stat([random.choice(groups) for _ in groups]) for _ in range(T))
    return out[int(.025 * T)], out[int(.975 * T)]


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit("使い方: where_we_lose.py <model.joblib> <from> <to>")
    mpath, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    if Path(mpath).resolve() == Path(PROD).resolve():
        raise SystemExit(
            "本番モデルは渡さないこと。評価期間を訓練に含むので in-sample になる。\n"
            "当日使われたモデルを git から取り出して渡すこと（先頭の説明を参照）。")
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    model = joblib.load(mpath)
    cols = _cols_for(model)
    print("モデル: %s" % mpath)

    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    X = df[cols].apply(pd.to_numeric, errors="coerce")
    # 列名に先頭アンダースコアを使わない（itertuples が別名に置き換えるため）
    df = df.assign(pl_score=model.predict(X.fillna(X.median()).values))

    from sqlalchemy import text, bindparam
    prm = {"d1": d1, "d2": d2, "bts": ["tansho", "nirenfuku"]}
    with get_engine().connect() as conn:
        od = conn.execute(text(
            "SELECT o.race_id,o.bet_type,o.combination,o.odds FROM odds o "
            "JOIN races r ON r.id=o.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND o.is_final=1 AND o.odds>0 AND o.bet_type IN :bts"
        ).bindparams(bindparam("bts", expanding=True)), prm).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no,rr.arrival_order FROM race_results rr "
            "JOIN races r ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order IS NOT NULL"
        ), prm).fetchall()

    odds = defaultdict(lambda: defaultdict(dict))
    for rid, bt, cb, o in od:
        odds[int(rid)][bt][str(cb)] = float(o)
    order = defaultdict(dict)
    for rid, bn, ao in res:
        order[int(rid)][int(ao)] = str(bn)

    need = {"tansho": 6, "nirenfuku": 15}
    g_boat, g_pair = [], []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        o = odds.get(rid)
        fin = order.get(rid)
        if not o or not fin or 1 not in fin or 2 not in fin:
            continue
        top1, top2 = fin[1], {fin[1], fin[2]}
        scores = {int(r.boat_no): float(r.pl_score) for r in grp.itertuples()}
        if len(scores) < 6:
            continue
        win = pl.scores_to_win_probs(scores, temperature=temp)
        exp_s = pl.to_exp_scores(scores, temperature=temp)

        # 艇単位（単勝）
        t = o.get("tansho", {})
        if len(t) == need["tansho"]:
            tot = sum(1.0 / v for v in t.values())
            rows = []
            for cb, v in t.items():
                p = win.get(int(cb))
                if p is None:
                    continue
                rows.append((p, (1.0 / v) / tot, cb == top1))
            if len(rows) == 6:
                g_boat.append(rows)

        # 組単位（2連複）
        nf = o.get("nirenfuku", {})
        if len(nf) == need["nirenfuku"]:
            tot = sum(1.0 / v for v in nf.values())
            rows = []
            for cb, v in nf.items():
                a, b = (int(x) for x in cb.split("-"))
                p = pl.joint_prob_nirenfuku(exp_s, a, b)
                rows.append((p, (1.0 / v) / tot, set(cb.split("-")) == top2))
            if len(rows) == 15:
                g_pair.append(rows)

    print("同一モデル・同一レースで、艇単位と組単位を市場と比べる")
    print("（%s 〜 %s / 温度 %.2f）" % (d1, d2, temp))
    print()
    print("単位          レース   行数   市場との差      95%区間（レース単位）")
    for label, g in (("艇（単勝）", g_boat), ("組（2連複）", g_pair)):
        if len(g) < 30:
            print("  %-12s データ不足 (%d レース)" % (label, len(g)))
            continue
        flat = [x for grp in g for x in grp]
        m, se = paired(flat)
        lo, hi = boot_by_race(g)
        print("  %-12s %6d %6d %+9.2f%% ±%.2f  [%+.2f%%, %+.2f%%]"
              % (label, len(g), len(flat), m, se, lo, hi))

    common = set(range(len(g_boat))) if False else None
    print()
    print("読み方:")
    print("  艇が互角で組だけ負けている → PL変換の作り直しが最短の改善")
    print("  どちらも同じだけ負けている → モデルそのものを良くするしかない")


if __name__ == "__main__":
    main()
