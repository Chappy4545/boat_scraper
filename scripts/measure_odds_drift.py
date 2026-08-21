"""締切20分前に見えていたオッズと、確定オッズのズレを測る。

混合ルールの検証は確定オッズで行っている。だが実際に買えるのは締切前の板で、
現在の運用は締切20分前に買い目を確定させる（is_final_pick）。
その20分でオッズが動くなら、検証の 186% はそのまま出ない。

docs/data/bets_<date>.json は 15 分ごとに書き換えられ、git に残っている。
各コミットがその時刻の板そのものなので、締切20分前に最も近いスナップショットを
拾えば「確定させた瞬間に見えていたオッズ」が復元できる。それを確定値と比べる。

ズレの向きが重要:
    確定 > 記録  → 見ていたより高く付いた（得）
    確定 < 記録  → 見ていたより低く付いた（損）
期待値で買う以上、系統的に下振れするなら回収率はそのぶん目減りする。

使い方:
    python scripts/measure_odds_drift.py [--before-min 20] [--dates 3]
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text                     # noqa: E402
from src.ingestion.database import get_engine, init_db   # noqa: E402
from src.utils.helpers import load_config       # noqa: E402

JST = timezone(timedelta(hours=9))


def sh(args: list[str]) -> bytes:
    return subprocess.run(args, capture_output=True, check=True).stdout


def snapshots(path: str) -> list[tuple[datetime, list]]:
    """そのファイルの全コミットを (時刻JST, 中身) で返す。"""
    out = []
    log = sh(["git", "log", "--format=%H %aI", "--reverse", "--", path]).decode()
    for line in log.split("\n"):
        if not line.strip():
            continue
        sha, iso = line.split()
        t = datetime.fromisoformat(iso).astimezone(JST)
        try:
            out.append((t, json.loads(sh(["git", "show", f"{sha}:{path}"]).decode("utf-8"))))
        except Exception:
            pass
    return out


def main() -> None:
    global MAX_GAP
    before = int(sys.argv[sys.argv.index("--before-min") + 1]) if "--before-min" in sys.argv else 20
    ndays = int(sys.argv[sys.argv.index("--dates") + 1]) if "--dates" in sys.argv else 3
    # 板の鮮度。15分間隔で更新しているので、既定は 8 分以内のものだけ使う。
    # 緩めると「20分前の板」ではなく「1時間前の板」を混ぜてしまい、
    # 発売直後の薄いオッズが紛れ込んで下振れを過大に見せる。
    MAX_GAP = int(sys.argv[sys.argv.index("--max-gap") + 1]) if "--max-gap" in sys.argv else 8

    init_db(load_config())
    engine = get_engine()

    files = sorted(Path("docs/data").glob("bets_2026-*.json"))[-ndays:]
    ratios, rows = [], []
    dropped: dict[str, int] = defaultdict(int)   # 板として成立せず捨てた賭式
    for f in files:
        d = f.stem.replace("bets_", "")
        snaps = snapshots(f"docs/data/{f.name}")
        if len(snaps) < 2:
            continue
        with engine.connect() as conn:
            close = {int(r[0]): str(r[1]) for r in conn.execute(text(
                "SELECT id, closing_time FROM races WHERE race_date = :d "
                "AND closing_time IS NOT NULL"), {"d": d})}
            final = defaultdict(dict)
            for rid, bt, cb, o in conn.execute(text(
                "SELECT o.race_id, o.bet_type, o.combination, o.odds FROM odds o "
                "JOIN races r ON r.id = o.race_id "
                "WHERE r.race_date = :d AND o.is_final = 1 AND o.odds > 0"), {"d": d}):
                final[int(rid)][(str(bt), str(cb))] = float(o)

        # 確定オッズ側の健全性を検査してから使う。
        # DB の確定オッズは朝の値が残ったまま一部だけ後から上書きされることが
        # あり、そうなると1つの賭式の中に別時点の値が混ざる。混ざった集合は
        # 板として成立しないので sum(1/オッズ) で弾ける（控除率25.8%なら
        # どの賭式でも 1.35 前後に必ず落ちる）。
        # 2026-08-21 実測: 若松 R2 の 2連複は 15通り中3通りが朝の値のままで
        # 合計 2.213 になっており、そのせいで「確定 2.2」という実在しない
        # 下振れが最大の例として出ていた。
        need = {"nirenfuku": 15, "nirentan": 30, "sanrenfuku": 20,
                "sanrentan": 120, "tansho": 6}
        for rid in list(final):
            by_type: dict[str, list[float]] = defaultdict(list)
            for (bt, _cb), o in final[rid].items():
                by_type[bt].append(o)
            for bt, odds_list in by_type.items():
                book = sum(1.0 / o for o in odds_list)
                if len(odds_list) != need.get(bt) or not (1.20 <= book <= 1.50):
                    dropped[bt] += 1
                    for key in [k for k in final[rid] if k[0] == bt]:
                        del final[rid][key]

        for rid, ct in close.items():
            try:
                cdt = datetime.strptime(f"{d} {ct}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
            except Exception:
                continue
            target = cdt - timedelta(minutes=before)
            # 確定させる時点(締切-20分)より前で、いちばん近いスナップショット
            cand = [(t, b) for t, b in snaps if t <= target]
            if not cand:
                continue
            t, bets = cand[-1]
            gap = (target - t).total_seconds() / 60
            if gap > MAX_GAP:     # 板が古すぎると「20分前の板」と言えない
                continue
            for b in bets:
                if b.get("race_id") != rid or not b.get("odds"):
                    continue
                fin = final.get(rid, {}).get((b["bet_type"], b["combination"]))
                if not fin:
                    continue
                ratios.append(fin / b["odds"])
                rows.append((d, b["combination"], b["odds"], fin, fin / b["odds"], gap,
                             b.get("rule") or "r5"))

    if not ratios:
        print("比較できるデータがありませんでした")
        return

    a = np.array(ratios)
    print(f"=== 締切{before}分前の板 vs 確定オッズ  {len(a):,}件 ===\n")
    print(f"  確定/記録 の中央値   {np.median(a):.3f}")
    print(f"  平均                {a.mean():.3f}")
    print(f"  下振れした割合       {(a < 1).mean() * 100:.1f}%")
    print(f"  5%以上 下振れ        {(a < 0.95).mean() * 100:.1f}%")
    print(f"  10%以上 下振れ       {(a < 0.90).mean() * 100:.1f}%")
    print()
    for q in (5, 25, 50, 75, 95):
        print(f"  {q:2d}パーセンタイル      {np.percentile(a, q):.3f}")
    print()
    print("  ※ 平均が 1.00 を下回るなら、見ていたオッズより低く付く傾向がある。")
    print(f"     期待値も回収率も、およそ {(a.mean() - 1) * 100:+.1f}% ぶん目減りする。")

    print("\n  日別（板の鮮度は最大 %d 分前まで）:" % MAX_GAP)
    by_day: dict[str, list] = defaultdict(list)
    for row in rows:
        by_day[row[0]].append(row[4])
    for d in sorted(by_day):
        v = np.array(by_day[d])
        print(f"    {d}: {len(v):3d}件  中央値{np.median(v):.3f}  "
              f"平均{v.mean():.3f}  下振れ{(v < 1).mean() * 100:.0f}%")

    # ルール別。混合ルールは「まだ誰も買っていない＝オッズが高い」買い目を
    # 狙う設計なので、締切間際に資金が入ると真っ先に潰れる。R5 は本命寄りで
    # 板が厚く動きにくい。同じ数字で語ると両方を読み違える。
    print("\n  ルール別:")
    by_rule: dict[str, list] = defaultdict(list)
    for row in rows:
        by_rule[row[6]].append(row[4])
    for name in sorted(by_rule):
        v = np.array(by_rule[name])
        print(f"    {name:14} {len(v):3d}件  中央値{np.median(v):.3f}  "
              f"平均{v.mean():.3f}  下振れ{(v < 1).mean() * 100:.0f}%")

    if dropped:
        print("\n  板として成立せず除外した確定オッズ（賭式ごとのレース数）:")
        for bt, n in sorted(dropped.items(), key=lambda x: -x[1]):
            print(f"    {bt:14} {n}レース")

    worst = sorted(rows, key=lambda r: r[4])[:5]
    print("\n  下振れが大きかった例:")
    for d, cb, rec, fin, r, gap, rule in worst:
        print(f"    {d} {cb:6} 記録{rec:6.1f} → 確定{fin:6.1f}  "
              f"({(r - 1) * 100:+.0f}%)  板の古さ{gap:.0f}分  {rule}")


if __name__ == "__main__":
    main()
