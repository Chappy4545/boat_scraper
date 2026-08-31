"""オッズの「動き」に情報があるかを測る。

なぜこれを測るのか
------------------
2026-08-30 までに4方向を測り切り、すべて否定的だった
（[[project_model_has_no_edge]]）。モデルは自明な基準すら上回らない。
残っているのは **モデルが一度も使っていない情報** で、それが「オッズの動き」。

    朝の板     薄い。発売直後。一般の買い手の見立て
    締切前の板  厚い。遅く入った金＝情報を持った金
    その差     市場の意見がどう変わったか

競馬・スポーツ賭博の文献では「遅い金は賢い」が繰り返し観測されている。
**ただしそれは市場の歪みではなく市場の効率の話**なので、控除率25%を
超えられるかは別問題。ここで測るのはそこ。

⚠️ 買える形で測る
-----------------
2026-08-30 に同じ罠に2回かかった。確定オッズ（レース後にしか分からない）で
区分を切ると良い数字が出るが、買えない。ここでは:

    区分に使うのは  朝の板 と 締切前の板（どちらも締切前に手に入る）
    払戻に使うのは  締切前の板（実際に買える値）

事前に決めた仮説（見る前に書く）
-------------------------------
    H1  縮んだ組合せ（drift < 0.9）の回収率 > 全部買う
    H2  伸びた組合せ（drift > 1.1）の回収率 < 全部買う
    H3  回収率は drift について単調（縮むほど良い）
    H4  drift で絞ると損益分岐(100%)を超える区分がある

H1〜H3 が成り立っても H4 が成り立たなければ「情報はあるが控除率に届かない」。
それでも意味はある（モデルの特徴量として使える）ので両方を報告する。

手順
----
前半の日で探し、後半の日で確かめる。区間はレース単位のブートストラップ
（同じレースの組合せは連動するので行単位だと誤差が小さく出すぎる）。

使い方
------
    python scripts/odds_drift.py
    python scripts/odds_drift.py --types tansho,sanrentan
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "docs" / "data"

# 朝の板・締切前の板の両方に十分な数が載る賭式。
# 複勝と拡連複は朝の板に無い（2026-08-31 に取り始めたばかり）。
DEFAULT_TYPES = ("tansho", "nirenfuku", "nirentan", "sanrenfuku", "sanrentan")


def load_morning(d: str) -> dict:
    """朝の板。(stadium_code, race_no, bet_type, combination) -> odds"""
    p = DATA / f"odds_raw_{d}.json.gz"
    if not p.exists():
        return {}
    j = json.load(gzip.open(p, "rt", encoding="utf-8"))
    return {(str(r["stadium_code"]), int(r["race_no"]),
             r["bet_type"], r["combination"]): float(r["odds"])
            for r in j.get("odds", []) if r.get("odds")}


def load_deadline(d: str, code_of: dict) -> dict:
    """締切前の板。board は場を名前で持つので、コードへ直す。"""
    p = DATA / f"board_{d}.json.gz"
    if not p.exists():
        return {}
    j = json.load(gzip.open(p, "rt", encoding="utf-8"))
    out = {}
    for _rid, r in j.get("races", {}).items():
        code = code_of.get(r.get("stadium"))
        if not code:
            continue
        rn = int(r.get("race_no"))
        for o in r.get("odds", []):
            if o.get("odds"):
                out[(code, rn, o["bet_type"], o["combination"])] = float(o["odds"])
    return out


def load_results() -> dict:
    """(race_date, stadium_code, race_no) -> 着順の艇番リスト（1着から）"""
    from src.ingestion.database import init_db, get_engine
    from src.utils.helpers import load_config
    from sqlalchemy import text as sa_text

    init_db(load_config())
    sql = """
        SELECT r.race_date, s.code, r.race_no, rr.arrival_order, rr.boat_no
          FROM race_results rr
          JOIN races r ON r.id = rr.race_id
          JOIN stadiums s ON s.id = r.stadium_id
         WHERE r.race_date >= :since AND rr.arrival_order BETWEEN 1 AND 3
         ORDER BY r.race_date, s.code, r.race_no, rr.arrival_order
    """
    out: dict = defaultdict(list)
    with get_engine().connect() as conn:
        for rd, code, rn, _order, boat in conn.execute(
                sa_text(sql), {"since": "2026-08-20"}).all():
            out[(str(rd), str(code), int(rn))].append(int(boat))
    return {k: v for k, v in out.items() if len(v) == 3}


def won(bet_type: str, combo: str, top3: list[int]) -> bool:
    a, b, c = top3
    if bet_type == "tansho":
        return combo == str(a)
    if bet_type == "nirentan":
        return combo == f"{a}-{b}"
    if bet_type == "nirenfuku":
        return set(map(int, combo.split("-"))) == {a, b}
    if bet_type == "sanrentan":
        return combo == f"{a}-{b}-{c}"
    if bet_type == "sanrenfuku":
        return set(map(int, combo.split("-"))) == {a, b, c}
    return False


def build(types) -> list[dict]:
    stadiums = json.loads((DATA / "stadiums.json").read_text(encoding="utf-8"))
    code_of = {s["name"]: str(s["code"]) for s in stadiums}
    results = load_results()

    rows = []
    for p in sorted(glob.glob(str(DATA / "board_*.json.gz"))):
        d = os.path.basename(p)[6:16]
        morning = load_morning(d)
        if not morning:
            continue
        deadline = load_deadline(d, code_of)
        for key, od in deadline.items():
            code, rn, bt, combo = key
            if bt not in types:
                continue
            om = morning.get(key)
            if not om or om <= 0 or od <= 0:
                continue
            top3 = results.get((d, code, rn))
            if not top3:
                continue
            rows.append({
                "date": d, "race": (d, code, rn), "bet_type": bt,
                "combo": combo, "om": om, "od": od,
                "drift": od / om,
                "won": won(bt, combo, top3),
            })
    return rows


def roi(rows) -> float:
    """100円ずつ買ったときの回収率。締切前のオッズで買う。"""
    if not rows:
        return float("nan")
    return float(np.mean([r["od"] if r["won"] else 0.0 for r in rows]))


def boot_ci(rows, n=2000, seed=0):
    """レース単位のブートストラップ。

    同じレースの組合せは連動する（1つ当たれば他は外れる）。行単位で
    抽出すると区間が狭く出すぎる。→ [[feedback_verify_every_change]] 7
    """
    if not rows:
        return (float("nan"), float("nan"))
    by_race = defaultdict(list)
    for r in rows:
        by_race[r["race"]].append(r)
    races = list(by_race.values())
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        idx = rng.integers(0, len(races), len(races))
        s = [x for i in idx for x in races[i]]
        out.append(roi(s))
    return (float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5)))


BUCKETS = [
    ("大きく縮んだ  <0.80", None, 0.80),
    ("縮んだ    0.80-0.95", 0.80, 0.95),
    ("ほぼ動かず 0.95-1.05", 0.95, 1.05),
    ("伸びた    1.05-1.25", 1.05, 1.25),
    ("大きく伸びた >1.25", 1.25, None),
]


def report(rows, label):
    print(f"\n=== {label} ===")
    if not rows:
        print("  データなし")
        return
    races = len({r['race'] for r in rows})
    base = roi(rows)
    lo, hi = boot_ci(rows)
    print(f"  全部買う  {base*100:6.1f}%  [{lo*100:.1f}〜{hi*100:.1f}]  "
          f"{len(rows):,}点 / {races}レース")
    print(f"  {'区分':22} {'回収率':>8} {'95%区間':>16} {'点数':>8} {'的中率':>7}")
    for name, lo_d, hi_d in BUCKETS:
        sub = [r for r in rows
               if (lo_d is None or r["drift"] >= lo_d)
               and (hi_d is None or r["drift"] < hi_d)]
        if len(sub) < 30:
            print(f"  {name:22} {'—':>8}  (点数不足 {len(sub)})")
            continue
        v = roi(sub)
        cl, ch = boot_ci(sub)
        hit = np.mean([r["won"] for r in sub])
        print(f"  {name:22} {v*100:7.1f}% [{cl*100:6.1f}〜{ch*100:6.1f}] "
              f"{len(sub):8,} {hit*100:6.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", default=",".join(DEFAULT_TYPES))
    a = ap.parse_args()
    types = set(a.types.split(","))

    rows = build(types)
    if not rows:
        print("対になるデータがありません")
        return
    days = sorted({r["date"] for r in rows})
    half = len(days) // 2
    win_a, win_b = set(days[:half]), set(days[half:])
    print(f"賭式: {sorted(types)}")
    print(f"日数 {len(days)}  探す窓 {sorted(win_a)}")
    print(f"          確かめる窓 {sorted(win_b)}")

    report([r for r in rows if r["date"] in win_a], "探す窓")
    report([r for r in rows if r["date"] in win_b], "確かめる窓")

    print("\n--- 賭式ごと（両窓まとめ。参考）---")
    for bt in sorted(types):
        sub = [r for r in rows if r["bet_type"] == bt]
        if len(sub) < 100:
            continue
        print(f"\n[{bt}]")
        report(sub, bt)


if __name__ == "__main__":
    main()
