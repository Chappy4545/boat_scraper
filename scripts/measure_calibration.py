"""out-of-sample 較正を「全組合せ」と「EVで選別した買い目」で比較する。

仮説: 平均の較正は良好でも、EV で選別した瞬間に予測確率が過大になる
（optimizer's curse）。EV = model_prob × odds なので、モデルの推定誤差が
上振れした組合せほど選ばれやすい。これが赤字の構造的原因かを判定する。

本番モデルには一切触れず、ranker を明示パスから読む。

使い方:
    python scratch_conditional_cal.py <ranker_path> <date_from> <date_to>
"""
from __future__ import annotations

import json
import logging
import sys
import time
import warnings
from collections import defaultdict

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src.features.builder import build_features, FEATURE_COLS
from src.models import plackett_luce as pl
from src.ingestion.database import get_engine
from src.utils.helpers import load_config

BET_TYPES = ["nirenfuku", "sanrenfuku", "sanrentan"]
BINS = [0.0, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15, 0.20, 0.30, 0.50, 1.01]
LABELS = ["0-1%", "1-2%", "2-3%", "3-5%", "5-7%", "7-10%",
          "10-15%", "15-20%", "20-30%", "30-50%", "50%+"]
MIN_EV = 1.2
MIN_ODDS, MAX_ODDS = 1.5, 50.0


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load_map(engine, sql, params, key_fn, val_fn):
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return rows


def main():
    ranker_path = sys.argv[1]
    date_from, date_to = sys.argv[2], sys.argv[3]

    cfg = load_config()
    temp = float(cfg.get("model", {}).get("pl_temperature", 1.0))
    engine = get_engine()

    log(f"ranker 読込: {ranker_path}")
    ranker = joblib.load(ranker_path)

    log(f"特徴量構築 {date_from} 〜 {date_to}")
    df = build_features(date_from, date_to, include_target=True)
    df = df.dropna(subset=["target_win"])
    log(f"  {len(df)} 行 / {df['race_id'].nunique()} レース")

    X = df[FEATURE_COLS].apply(pd.to_numeric, errors="coerce")
    X = X.fillna(X.median()).values
    df = df.assign(_score=ranker.predict(X))

    log("payouts / odds 読込")
    from sqlalchemy import text, bindparam
    q_pay = text("SELECT p.race_id,p.bet_type,p.combination FROM payouts p "
                 "JOIN races r ON r.id=p.race_id "
                 "WHERE r.race_date BETWEEN :d1 AND :d2 AND p.bet_type IN :bts"
                 ).bindparams(bindparam("bts", expanding=True))
    q_odds = text("SELECT o.race_id,o.bet_type,o.combination,o.odds FROM odds o "
                  "JOIN races r ON r.id=o.race_id "
                  "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.is_final=1 "
                  "AND o.bet_type IN :bts"
                  ).bindparams(bindparam("bts", expanding=True))
    prm = {"d1": date_from, "d2": date_to, "bts": BET_TYPES}
    with engine.connect() as conn:
        pay_rows = conn.execute(q_pay, prm).fetchall()
        odds_rows = conn.execute(q_odds, prm).fetchall()

    hits = defaultdict(set)
    for rid, bt, cb in pay_rows:
        hits[(int(rid), bt)].add(str(cb))
    odds = {}
    for rid, bt, cb, od in odds_rows:
        odds[(int(rid), bt, str(cb))] = float(od)
    races_with_odds = {k[0] for k in odds}
    log(f"  payouts={len(pay_rows)} odds={len(odds_rows)} "
        f"odds実在レース={len(races_with_odds)}")

    # [n, hits, sum_p] を 全体 / 選別後 それぞれで帯別に累積
    acc_all = {bt: {i: [0, 0, 0.0] for i in range(len(LABELS))} for bt in BET_TYPES}
    acc_sel = {bt: {i: [0, 0, 0.0] for i in range(len(LABELS))} for bt in BET_TYPES}
    # 選別後の投資回収（100円賭け換算）
    money = {bt: [0.0, 0.0, 0, 0] for bt in BET_TYPES}  # stake, ret, n, hit
    # 帯別の投資回収（どの帯なら黒字かを見る）
    money_band = {bt: {i: [0.0, 0.0] for i in range(len(LABELS))} for bt in BET_TYPES}

    log("PL 確率計算 → 全体/選別 で集計")
    n = 0
    for race_id, g in df.groupby("race_id", sort=False):
        rid = int(race_id)
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_score"])}
        if len(scores) < 6:
            continue
        probs = pl.all_bet_probs(scores, temperature=temp)
        has_odds = rid in races_with_odds
        for bt in BET_TYPES:
            hs = hits.get((rid, bt))
            if not hs:
                continue
            for item in probs.get(bt, []):
                p = float(item["model_prob"])
                cb = item["combination"]
                y = 1 if cb in hs else 0
                idx = max(0, min(int(np.digitize(p, BINS) - 1), len(LABELS) - 1))
                a = acc_all[bt][idx]
                a[0] += 1; a[1] += y; a[2] += p

                if not has_odds:
                    continue
                od = odds.get((rid, bt, cb))
                if od is None or not (MIN_ODDS <= od <= MAX_ODDS):
                    continue
                if p * od < MIN_EV:
                    continue
                s = acc_sel[bt][idx]
                s[0] += 1; s[1] += y; s[2] += p
                m = money[bt]
                m[0] += 100.0; m[2] += 1
                mb = money_band[bt][idx]
                mb[0] += 100.0
                if y:
                    m[1] += 100.0 * od; m[3] += 1
                    mb[1] += 100.0 * od
        n += 1
        if n % 1000 == 0:
            log(f"  {n} レース")

    def bands(acc, bt, with_money=False):
        out = []
        for i, lab in enumerate(LABELS):
            c, h, ps = acc[bt][i]
            if c == 0:
                continue
            row = {"band": lab, "n": c, "hits": h,
                   "pred": ps / c, "actual": h / c,
                   "ratio": (h / c) / (ps / c) if ps > 0 else None}
            if with_money:
                st, rt = money_band[bt][i]
                row["stake"] = st
                row["return"] = rt
                row["roi"] = rt / st if st else None
            out.append(row)
        return out

    res = {"races": n, "date_from": date_from, "date_to": date_to,
           "min_ev": MIN_EV, "by_bet_type": {}}
    for bt in BET_TYPES:
        st, rt, cnt, hit = money[bt]
        res["by_bet_type"][bt] = {
            "all": bands(acc_all, bt),
            "selected": bands(acc_sel, bt, with_money=True),
            "selected_money": {
                "bets": cnt, "hits": hit,
                "hit_rate": hit / cnt if cnt else None,
                "stake": st, "return": rt,
                "roi": rt / st if st else None,
            },
        }
    with open("scratch_conditional_cal.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    for bt in BET_TYPES:
        d = res["by_bet_type"][bt]
        m = d["selected_money"]
        log(f"=== {bt} ===")
        log(f"  [選別後] bets={m['bets']} hits={m['hits']} "
            f"hit_rate={(m['hit_rate'] or 0)*100:.2f}% ROI={(m['roi'] or 0)*100:.1f}%")
        tn = sum(r["n"] for r in d["all"])
        tp = sum(r["pred"] * r["n"] for r in d["all"])
        th = sum(r["hits"] for r in d["all"])
        log(f"  [全組合せ] n={tn} 予測平均={tp/tn*100:.3f}% 実際={th/tn*100:.3f}% "
            f"比={(th/tn)/(tp/tn) if tp else 0:.2f}")
        sn = sum(r["n"] for r in d["selected"])
        if sn:
            sp = sum(r["pred"] * r["n"] for r in d["selected"])
            sh = sum(r["hits"] for r in d["selected"])
            log(f"  [選別後]   n={sn} 予測平均={sp/sn*100:.3f}% 実際={sh/sn*100:.3f}% "
                f"比={(sh/sn)/(sp/sn) if sp else 0:.2f}  ← 核心")
    log("DONE")


if __name__ == "__main__":
    main()
