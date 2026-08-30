"""賭式を変えると勝てるのか。2連複 / 3連複 / 3連単 を同じレースで比べる。

なぜ測るか
--------
2連複では、実装可能な条件で控除率25.8%を超えるものが見つからなかった
（ウォークフォワード14,187レース / 実運用401本 / 締切前の板1,032レース）。

    無作為に買った場合   74.2%
    モデルの1点          75〜91%（期間による）

3連単は120通りで控除率25.2%。組合せが多いぶん市場の値付けが甘い場所が
残りやすい、というのが仮説。**モデルは同じ**（艇のスコア）で、
Plackett-Luce で組合せ確率に変換する部分だけが変わる。

⚠️ 変換にはコストがある。2連複で 5.32% 失うと実測済み
（[[project_pl_transform_cost]]）。3連単は変換がさらに重いので、
控除率が 0.6pt 低いぶんを食い潰す可能性が高い。それも含めて測る。

設計
----
- 打ち切りまでで訓練 → 翌日から20日を予測。1日も重ねない
- **同じレースの上で**賭式を比べる（全通り揃っている板だけ）
- モデルは1つ。賭式ごとに確率変換だけ変える
- 独立2窓（前半3打ち切り / 後半3打ち切り）

使い方:
    python scripts/wf_bet_types.py
"""
from __future__ import annotations

import random
import sys
from collections import defaultdict
from itertools import combinations, permutations
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from src.features.builder import build_features, FEATURE_COLS   # noqa: E402
from src.models import plackett_luce as pl                      # noqa: E402
from src.ingestion.database import get_engine, init_db          # noqa: E402
from src.utils.helpers import load_config                       # noqa: E402

TRAIN_FROM = "2026-01-01"
HORIZON = 20
DEFAULT_CUTOFFS = ["2026-05-01", "2026-05-21", "2026-06-10",
                   "2026-06-30", "2026-07-20", "2026-08-09"]
# 賭式 -> (全通りの数, 控除後の取り分＝無作為に買ったときの期待回収)
#
# 取り分は実測（sum(1/確定オッズ) の中央値の逆数）。額面の25%とは一致しない。
# ⚠️ 単勝がいちばん悪い。オッズの刻みが粗く 1.0 倍の下限があるためとみられる。
# ただし単勝だけ **Plackett-Luce の変換が要らない**（モデルの1着確率がそのまま
# 使える）。2連複では変換だけで 5.32% 失うと実測済みなので、
# 控除率で 1.2pt 損して変換コストを得する取引になる。だから測る価値がある。
ALL_SPECS = {"tansho": (6, 0.736), "nirenfuku": (15, 0.742),
             "sanrenfuku": (20, 0.744), "sanrentan": (120, 0.748)}
# ⚠️ 単勝の確定オッズは 2026-07-05 以降しか無い。既定から外し、
#    --types で指定したときだけ対象にする（打ち切りも合わせること）。
SPECS = {k: v for k, v in ALL_SPECS.items() if k != "tansho"}


def train_at(cutoff, seed):
    df = build_features(TRAIN_FROM, cutoff, include_target=True)
    df = df.dropna(subset=["arrival_order"])
    df = df[df["arrival_order"] > 0]
    if df.empty:
        return None, 0
    df = df.sort_values(["race_date", "race_id", "boat_no"]).reset_index(drop=True)
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce").values.astype(float)
    y = (4 - df["arrival_order"].astype(int).clip(1, 4)).clip(0, 3).values
    groups = df.groupby("race_id", sort=False).size().values
    med = np.nanmedian(X, axis=0)
    X = np.where(np.isnan(X), med, X)
    r = lgb.LGBMRanker(
        objective="lambdarank", n_estimators=500, learning_rate=0.05,
        max_depth=6, num_leaves=31, subsample=0.8, colsample_bytree=0.8,
        min_child_samples=20, random_state=seed, n_jobs=-1, verbose=-1,
        label_gain=[0, 1, 3, 7])
    r.fit(X, y, group=groups)
    r._medians = med
    return r, df["race_id"].nunique()


def probs_for(exp_s, bt):
    """賭式ごとの全組合せ確率。組の表記は payouts / odds と同じ並びにする。"""
    boats = sorted(exp_s)
    out = {}
    if bt == "tansho":
        # 変換なし。Plackett-Luce の1着確率そのもの＝モデルの素の出力
        tot = sum(exp_s.values())
        return {str(b): exp_s[b] / tot for b in boats}
    if bt == "nirenfuku":
        for a, b in combinations(boats, 2):
            out[f"{a}-{b}"] = pl.joint_prob_nirenfuku(exp_s, a, b)
    elif bt == "sanrenfuku":
        for a, b, c in combinations(boats, 3):
            out[f"{a}-{b}-{c}"] = pl.joint_prob_sanrenfuku(exp_s, a, b, c)
    else:
        for a, b, c in permutations(boats, 3):
            out[f"{a}-{b}-{c}"] = pl.joint_prob_sanrentan(exp_s, a, b, c)
    return out


def winner_of(fin, bt):
    """着順から当たりの組を作る。"""
    if 1 not in fin:
        return None
    if bt == "tansho":
        return str(fin[1])
    if 2 not in fin:
        return None
    if bt == "nirenfuku":
        return "-".join(map(str, sorted([fin[1], fin[2]])))
    if 3 not in fin:
        return None
    if bt == "sanrenfuku":
        return "-".join(map(str, sorted([fin[1], fin[2], fin[3]])))
    return f"{fin[1]}-{fin[2]}-{fin[3]}"


def evaluate(model, d1, d2):
    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    df = build_features(d1, d2, include_target=True).dropna(subset=["target_win"])
    if df.empty:
        return []
    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce").values.astype(float)
    X = np.where(np.isnan(X), model._medians, X)
    df = df.assign(score=model.predict(X))

    from sqlalchemy import text
    with get_engine().connect() as conn:
        od = conn.execute(text(
            "SELECT o.race_id,o.bet_type,o.combination,o.odds FROM odds o "
            "JOIN races r ON r.id=o.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND o.is_final=1 AND o.odds>0"), {"d1": d1, "d2": d2}).fetchall()
        res = conn.execute(text(
            "SELECT rr.race_id,rr.boat_no,rr.arrival_order FROM race_results rr "
            "JOIN races r ON r.id=rr.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND rr.arrival_order IS NOT NULL"), {"d1": d1, "d2": d2}).fetchall()

    board = defaultdict(lambda: defaultdict(dict))
    for rid, bt, cb, o in od:
        if bt in SPECS:
            board[int(rid)][bt][str(cb)] = float(o)
    order = defaultdict(dict)
    for rid, bn, ao in res:
        order[int(rid)][int(ao)] = int(bn)

    rows = []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        bo, fin = board.get(rid, {}), order.get(rid, {})
        # 全賭式そろっているレースだけ（対で比べるため）
        if any(len(bo.get(bt, {})) != n for bt, (n, _) in SPECS.items()):
            continue
        scores = {int(r.boat_no): float(r.score) for r in grp.itertuples()}
        if len(scores) < 6:
            continue
        exp_s = pl.to_exp_scores(scores, temperature=temp)
        row = {"race_id": rid}
        ok = True
        for bt in SPECS:
            win = winner_of(fin, bt)
            if win is None:
                ok = False
                break
            p = probs_for(exp_s, bt)
            top = max(p, key=p.get)
            row[bt] = {"pick": top, "p": p[top], "odds": bo[bt].get(top, 0.0),
                       "ret": bo[bt].get(win, 0.0) if top == win else 0.0,
                       "hit": int(top == win)}
        if ok:
            rows.append(row)
    return rows


def boot(vals, T=2000):
    if len(vals) < 30:
        return None, None
    random.seed(0)
    v = []
    for _ in range(T):
        s = [random.choice(vals) for _ in vals]
        v.append(sum(s) / len(s) * 100)
    v.sort()
    return v[int(.025 * T)], v[int(.975 * T)]


def report(label, rows):
    print(f"  {label}  {len(rows)}レース")
    for bt, (_n, keep) in SPECS.items():
        r = [x[bt] for x in rows]
        n = len(r)
        if not n:
            continue
        hit = sum(x["hit"] for x in r) / n * 100
        roi = sum(x["ret"] for x in r) / n * 100
        lo, hi = boot([x["ret"] for x in r])
        ci = f" [95% {lo:.0f}〜{hi:.0f}]" if lo is not None else ""
        base = keep * 100
        lift = roi - base
        avg = sum(x["odds"] for x in r) / n
        mark = "  ★100%超" if lo is not None and lo > 100 else ""
        print(f"     {bt:<11} 的中{hit:5.2f}%  回収{roi:6.1f}%{ci}"
              f"  無作為{base:.1f}% 差{lift:+5.1f}pt  平均{avg:6.1f}倍{mark}")


def main():
    global SPECS
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoffs", help="打ち切り日をカンマ区切りで")
    ap.add_argument("--types", help="賭式をカンマ区切りで（tansho を含められる）")
    a = ap.parse_args()
    cutoffs = a.cutoffs.split(",") if a.cutoffs else DEFAULT_CUTOFFS
    if a.types:
        SPECS = {k: ALL_SPECS[k] for k in a.types.split(",")}
    globals()["CUTOFFS"] = cutoffs

    init_db(load_config())
    seed = load_config()["model"].get("random_state", 42)
    print("賭式の比較（同じレース・同じモデル・確率変換だけ変える）")
    print(f"対象 {list(SPECS)}")
    print(f"打ち切り {cutoffs}")
    print()
    per = {}
    for cu in cutoffs:
        d1 = (pd.Timestamp(cu) + pd.Timedelta(days=1)).date().isoformat()
        d2 = (pd.Timestamp(cu) + pd.Timedelta(days=HORIZON)).date().isoformat()
        m, ntr = train_at(cu, seed)
        if m is None:
            continue
        rows = evaluate(m, d1, d2)
        per[cu] = rows
        print(f"打ち切り {cu}（訓練 {ntr}レース）→ 予測 {d1}〜{d2}")
        report("この窓", rows)
        print()

    half = len(cutoffs) // 2
    for name, cus in (("窓A（前半）", cutoffs[:half]), ("窓B（後半）", cutoffs[half:])):
        rows = [x for c in cus for x in per.get(c, [])]
        if rows:
            print(f"=== {name} {cus} ===")
            report("合計", rows)
            print()
    print("判定: 無作為との差(pt)が **両窓とも +25pt 以上** で初めて損益分岐に届く。")


if __name__ == "__main__":
    main()
