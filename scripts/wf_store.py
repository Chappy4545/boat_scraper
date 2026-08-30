"""ウォークフォワードの予測を1回だけ回し、切り直せる形で保存する。

なぜ保存するか
------------
区分探しは何度も切り直す作業なのに、そのたびに6打ち切りぶんの訓練
（20分以上）をやり直していた。1回走らせて保存すれば、以後の分析は数秒で済む。

保存するのは「賭式ごとに確率が最大の1点」と、その1点を切り分けるための
**買う時点で分かる情報だけ**（場・グレード・レース番号・ナイター・
1号艇の級別と全国勝率・モデルの確率と1位2位の差）。

⚠️ オッズは**入れない**。2026-08-30 だけで3回、確定オッズで区分を切って
「勝てる」と誤認した（edge>=2.0 142%→54%、1号艇の人気薄、単勝）。
オッズが手元に無ければその罠は構造的に起きない。
成績は payouts から出すので、オッズが無くても回収率は測れる。

出力: data/processed/wf_picks.db（.gitignore 済・再生成可）
    picks(cutoff, race_date, stadium, race_no, grade, is_night,
          bet_type, combination, model_prob, gap, hit, ret,
          b1_class, b1_win_rate)

使い方:
    python scripts/wf_store.py
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

from src.features.builder import build_features, FEATURE_COLS   # noqa: E402
from src.models import plackett_luce as pl                      # noqa: E402
from src.ingestion.database import get_engine, init_db          # noqa: E402
from src.utils.helpers import load_config                       # noqa: E402
from wf_bet_types import train_at, HORIZON                      # noqa: E402
from wf_all_bet_types import SPECS, PAYOUT_KEY, probs_for       # noqa: E402

OUT = Path("data/processed/wf_picks.db")
CUTOFFS = ["2026-05-01", "2026-05-21", "2026-06-10",
           "2026-06-30", "2026-07-20", "2026-08-09"]

DDL = """
CREATE TABLE IF NOT EXISTS picks(
  cutoff TEXT, race_date TEXT, stadium TEXT, race_no INT, grade TEXT,
  is_night INT, bet_type TEXT, combination TEXT, model_prob REAL,
  gap REAL, hit INT, ret REAL, b1_class TEXT, b1_win_rate REAL
)
"""


def collect(model, cutoff, d1, d2):
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
        pay = conn.execute(text(
            "SELECT p.race_id,p.bet_type,p.combination,p.payout FROM payouts p "
            "JOIN races r ON r.id=p.race_id WHERE r.race_date BETWEEN :d1 AND :d2"),
            {"d1": d1, "d2": d2}).fetchall()
        meta = conn.execute(text(
            "SELECT r.id, s.name, r.race_no, r.race_date, r.grade, r.is_night "
            "FROM races r JOIN stadiums s ON s.id=r.stadium_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2"), {"d1": d1, "d2": d2}).fetchall()
        ent = conn.execute(text(
            "SELECT e.race_id, e.racer_class, e.national_win_rate FROM race_entries e "
            "JOIN races r ON r.id=e.race_id WHERE r.race_date BETWEEN :d1 AND :d2 "
            "AND e.boat_no=1"), {"d1": d1, "d2": d2}).fetchall()

    won = defaultdict(lambda: defaultdict(dict))
    for rid, bt, cb, p in pay:
        won[int(rid)][str(bt)][str(cb)] = (p or 0) / 100.0
    minfo = {int(r[0]): r[1:] for r in meta}
    e1 = {int(r[0]): (r[1], r[2]) for r in ent}

    rows = []
    for rid, grp in df.groupby("race_id"):
        rid = int(rid)
        w, m = won.get(rid), minfo.get(rid)
        if not w or not m:
            continue
        scores = {int(r.boat_no): float(r.score) for r in grp.itertuples()}
        if len(scores) < 6:
            continue
        exp_s = pl.to_exp_scores(scores, temperature=temp)
        stadium, race_no, race_date, grade, is_night = m
        cls, wr = e1.get(rid, (None, None))
        out = []
        ok = True
        for bt, (_nc, nwin) in SPECS.items():
            hits = w.get(PAYOUT_KEY.get(bt, bt), {})
            if len(hits) != nwin:
                ok = False
                break
            p = probs_for(exp_s, bt)
            ordered = sorted(p.items(), key=lambda x: -x[1])
            top, ptop = ordered[0]
            gap = ptop - (ordered[1][1] if len(ordered) > 1 else 0.0)
            out.append((cutoff, str(race_date), stadium, int(race_no), grade or "一般",
                        int(bool(is_night)), bt, top, float(ptop), float(gap),
                        int(top in hits), float(hits.get(top, 0.0)), cls, wr))
        if ok:
            rows.extend(out)
    return rows


def main():
    init_db(load_config())
    seed = load_config()["model"].get("random_state", 42)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(OUT)
    con.execute("DROP TABLE IF EXISTS picks")
    con.execute(DDL)
    total = 0
    for cu in CUTOFFS:
        d1 = (pd.Timestamp(cu) + pd.Timedelta(days=1)).date().isoformat()
        d2 = (pd.Timestamp(cu) + pd.Timedelta(days=HORIZON)).date().isoformat()
        m, ntr = train_at(cu, seed)
        if m is None:
            continue
        rows = collect(m, cu, d1, d2)
        # 列数が合っているか毎回確かめる（過去に placeholder 数を間違えて
        # 訓練1回ぶんを無駄にしたことがある）
        ncol = len(con.execute("SELECT * FROM picks LIMIT 0").description)
        assert not rows or len(rows[0]) == ncol, f"列数不一致 {len(rows[0])} != {ncol}"
        con.executemany(f"INSERT INTO picks VALUES ({','.join('?' * ncol)})", rows)
        con.commit()
        total += len(rows)
        print(f"打ち切り {cu}（訓練 {ntr}レース）→ {d1}〜{d2}  {len(rows)}行")
    print(f"\n保存: {OUT}  合計 {total}行 / "
          f"{con.execute('SELECT COUNT(DISTINCT race_date||stadium||race_no) FROM picks').fetchone()[0]}レース")
    con.close()


if __name__ == "__main__":
    main()
