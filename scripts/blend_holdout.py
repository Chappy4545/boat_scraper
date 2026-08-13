"""事前登録したルールを、閾値選定に使っていないレースだけで測る。

閾値 p>=0.10 は 141 本の標本を見て決めた。同じ標本を含む集合で測り直しても
「選んだ条件が選んだデータで良い」ことしか言えない。

今日（2026-08-13）のバックフィルで、確定オッズが揃ったレースが
4,732 → 14,917 に増えた。**新しく揃った 1 万レースは閾値選定に
一切関与していない。** odds.recorded_at で切り分けられる。

    選定に使った分 : 2026-08-13 より前に記録された確定オッズ
    純粋な検証用   : 2026-08-13 に記録された確定オッズ

後者だけでの成績が、このルールの本当の実力に最も近い。

使い方:
    python scripts/blend_holdout.py <ranker> <from> <to>
        [--w 0.3] [--p 0.10] [--ev 1.2] [--split 2026-08-13] [--boot 2000]
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

from sqlalchemy import text                       # noqa: E402
from src.ingestion.database import get_engine     # noqa: E402
from scripts.blend_folds import load              # noqa: E402
from scripts.blend_bet_rules import bootstrap_roi  # noqa: E402


def arg(name: str, default: float) -> float:
    return float(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


# そのレースの確定オッズ 15 通りが、いつ揃ったか（最も遅い記録時刻で判定）
WHEN = """
    SELECT race_id, MAX(recorded_at) AS completed
      FROM odds
     WHERE bet_type = 'nirenfuku' AND is_final = 1 AND odds > 0
     GROUP BY race_id
    HAVING COUNT(*) = 15
"""


def main() -> None:
    ranker, d1, d2 = sys.argv[1], sys.argv[2], sys.argv[3]
    w, pmin, evmin = arg("--w", 0.3), arg("--p", 0.10), arg("--ev", 1.2)
    n_boot = int(arg("--boot", 2000))
    split = (sys.argv[sys.argv.index("--split") + 1]
             if "--split" in sys.argv else "2026-08-13")
    rng = np.random.default_rng(0)

    with get_engine().connect() as conn:
        when = {int(r[0]): str(r[1]) for r in conn.execute(text(WHEN))}

    R = load(ranker, d1, d2)
    R["completed"] = R["race_id"].map(when)
    R["group"] = np.where(R["completed"].fillna("") >= split, "新規", "選定に使用")

    R["pb"] = w * R["pm"] + (1 - w) * R["pk"]
    R["ev"] = R["pb"] * R["odds"]
    sel = R[(R["pb"] >= pmin) & (R["ev"] >= evmin)]

    print(f"=== 事前登録ルール: 混合(モデル{w * 100:.0f}%) p>={pmin} & EV>={evmin} ===")
    print(f"{d1}〜{d2}  分割日 {split}\n")
    print(f"{'区分':22}{'レース':>8}{'本数':>7}{'的中率':>8}{'回収率':>8}"
          f"{'95%区間':>18}{'最大1本':>9}")

    for label in ("選定に使用", "新規", "全体"):
        s = sel if label == "全体" else sel[sel["group"] == label]
        races = (R if label == "全体" else R[R["group"] == label])["race_id"].nunique()
        n = len(s)
        if n < 20:
            print(f"{label:22}{races:>8,}{n:>7,}  （少なすぎて判断不能）")
            continue
        pays = (s["y"] * s["odds"]).values
        hr = s["y"].mean()
        roi = pays.sum() / n * 100
        share = pays.max() / pays.sum() * 100 if pays.sum() > 0 else 0
        lo, hi, _ = bootstrap_roi(s, n_boot, rng)
        flag = "  ← 100%超" if lo > 100 else ""
        if label == "全体":
            print("─" * 80)
        print(f"{label:22}{races:>8,}{n:>7,}{hr * 100:>7.1f}%{roi:>7.0f}%"
              f"{f'[{lo:.0f}%, {hi:.0f}%]':>18}{share:>8.0f}%{flag}")

    print("\n※「新規」= 今日バックフィルで初めて使えるようになったレース。")
    print("   閾値を決めたときには存在しなかったので、ここが本当の検証。")


if __name__ == "__main__":
    main()
