"""モデルを市場より賢くできるか。5案を未見データで一度だけ比べる。

⚠️ 事前登録（2026-09-03・**結果を1つも見る前に書いてコミットする**）
==================================================================
仮説・比べ方・判定基準・停止条件をここで固定する。実行して結果を見た後に
この docstring を書き換えない。書き換えたら事前登録の意味が消える。

なぜやり直すのか — 2026-08-12 の不採用は信用できない
----------------------------------------------------
同じ趣旨の実験が `e7b1fcb` で行われ、「市場を入れると悪化する」と結論した:

    A 現行モデル        対数損失 0.18771  ← 最良とされた
    -  市場そのもの              0.19445
    B 市場込みで予測            0.20331
    C 市場の誤りを学習          0.20957

**この比較は成立していない。3つの欠陥がある:**

1. ⚠️⚠️ **A だけが in-sample だった。**
   `scripts/test_market_model.py` は A の確率を `load_ranker()`（本番モデル）
   で作る。当時の本番モデルは実験当日の朝 09:43 に訓練されており
   （`training_summary_20260812_094334`）、検証期間 07-01〜08-11 を
   **訓練データに含む**。一方 B・C は 05-01〜06-30 だけで訓練して
   07-01 以降を予測している。**A だけが答えを見ていた。**
2. 検証が **854レース**しかない（`is_live=1` のオッズがそれだけだった）。
3. その実験自身の基準「現行モデルが市場を上回る(0.18771 < 0.19445)」は
   **翌日 08-13 に否定されている**（未見データで市場が 3.0% ±0.9 優位）。
   基準が誤っていた実験の結論は、そのままでは使えない。

いま市場確率を作れるレースは **18,307**（2連複15通り+着順、5〜9月）で20倍。
→ [[project_backtest_leak]] [[project_calibration_priority]]

比べるもの
----------
すべて**同一のウォークフォワード枠・同一レース集合**で、
**5案とも自前で訓練する**（`load_ranker()` は使わない。上記1の再発防止）。

    A 現行         FEATURE_COLS 34項目のみ                     基準線
    B 市場込み     34 + 市場の含意確率（2連複から艇別に作る）    08-12の再測定
    C 残差学習     市場確率からのズレを学習                      08-12の再測定
    D 直前情報込み 34 + EXTRA_FEATURE_COLS 13項目               既に+0.52%の実績
    E 二段階補正   ⭐**未実施・主判定**

E の作り: 土台モデルは**市場を一度も見ない**（＝Aと同じ特徴量）。
後段で logit(p_model) と logit(p_market) を合成する校正器だけを、
**土台の訓練期間とも検証期間とも重ならない期間**で当てる。
B/C が負けた原因が「モデルが市場に従うことを学習する」なら、
E は土台が市場を見ないので**構造上その負け方をしない**。
既存の固定ブレンド（p=0.3*model+0.7*market）とも別物（あれは学習しない）。
→ [[project_blend_candidate]]

⚠️ A と B/C は訓練できる期間が違う（B/C はオッズのある5月以降）。
**B/C と比べるときは A も同じ部分集合に制限する。**
しないと「市場の効果」と「訓練データ量の差」が混ざる。

市場の含意確率の作り方
----------------------
2連複の板から、賭式内で正規化して控除率を除く:

    P_market({i,j}) = (1/odds_ij) / Σ_kl (1/odds_kl)
    艇ごと:  P(艇i が2着以内) = Σ_{j≠i} P_market({i,j})     6艇の和は 2.00

判定（先に決める）
------------------
    主判定  E の対数損失 < 市場の対数損失
            レース単位ブートストラップの95%区間が0を跨がないこと
            ⚠️ レース単位で取る。同じレースの15通りは連動するので
               行単位のSEは小さく出すぎる
    副判定  D も同じ形で見る。2件検定するので α=0.025 ずつ（Bonferroni）
    参考    A / B / C は表に出すだけ。**判定には使わない**
    併記    ⭐**複勝の最上位帯だけに絞った対数損失も必ず出す。**
            実質控除率が約2%の唯一の区域なので、全体で負けていても
            ここで勝てていれば意味がある（逆もある）
            → [[project_fukusho_floor]]

    ⚠️ 結果を見てから案を足さない。賭式を増やさない。期間をずらさない。
    ⚠️ 回収率で判定しない（誤差±5〜15pt。対数損失は1桁小さい）

停止条件
--------
どれも市場を下回らなければ、**この方向は打ち切る**と明記して報告する。
「惜しい」「もう少しデータがあれば」を理由に条件を足さない。

使い方
------
    python scripts/market_trial.py                 # 既定のウォークフォワード
    python scripts/market_trial.py --quick         # 打ち切り1つだけ（動作確認）
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from src.features.builder import (  # noqa: E402
    EXTRA_FEATURE_COLS, FEATURE_COLS, build_features,
)
from src.ingestion.database import get_engine  # noqa: E402
from src.models import plackett_luce as pl  # noqa: E402

BT = "nirenfuku"
NEED = 15                      # 2連複の全通り
EPS = 1e-9


# ── 市場の含意確率 ────────────────────────────────
def load_market(d1: str, d2: str, source: str = "final") -> dict[int, dict[str, float]]:
    """レースごとの {組合せ: 市場確率}。全通り揃ったレースだけ返す。

    source は **必ず明示する**。混ぜてはいけない。
      "final" … 締切時点の確定オッズ（is_final=1）
      "board" … その日に取った板（is_final=0）

    ⚠️⚠️ 2026-09-03、最初これを `is_final` で絞らずに書いた。同じ
    (race_id, 組合せ) に両方の行があると（実測 14,487組）**辞書が後勝ちで
    別時点の値を混ぜる**。合計は1になるので気づけず、
    `sum(1/オッズ)` を見て初めて 8.36% が 1.6 超と分かった
    （源を分けると両方とも 1.35・1.6超は0件）。
    2026-08-21 の DB 破損と同じ「別時点の混合」の再現。**必ず源で絞る。**

    ⚠️ 1通りでも欠けると正規化が壊れるので、揃っていないレースは丸ごと捨てる。
    """
    if source not in ("final", "board"):
        raise ValueError(f"source は final か board: {source!r}")
    is_final = 1 if source == "final" else 0
    with get_engine().connect() as c:
        rows = c.execute(text(
            "SELECT o.race_id, o.combination, o.odds FROM odds o "
            "JOIN races r ON r.id = o.race_id "
            "WHERE r.race_date BETWEEN :d1 AND :d2 AND o.bet_type = :bt "
            "AND o.odds > 0 AND o.is_final = :f"),
            {"d1": d1, "d2": d2, "bt": BT, "f": is_final}).fetchall()
    per: dict[int, dict[str, float]] = defaultdict(dict)
    for rid, cb, o in rows:
        per[int(rid)][str(cb)] = float(o)
    out = {}
    for rid, od in per.items():
        if len(od) != NEED:
            continue
        inv = {cb: 1.0 / o for cb, o in od.items()}
        tot = sum(inv.values())
        # 控除率どおりでない＝別時点の混合か欠損。使わない。
        if not (1.15 <= tot <= 1.55):
            continue
        out[rid] = {cb: v / tot for cb, v in inv.items()}
    return out


def boat_market(mkt_race: dict[str, float]) -> dict[int, float]:
    """組合せの市場確率 → 艇ごとの「2着以内」確率。6艇の和は 2.00。"""
    per = defaultdict(float)
    for cb, p in mkt_race.items():
        a, b = (int(x) for x in cb.split("-"))
        per[a] += p
        per[b] += p
    return dict(per)


# ── 評価 ──────────────────────────────────────────
def logloss_by_race(p: np.ndarray, y: np.ndarray, race: np.ndarray) -> dict:
    """レース単位に平均した対数損失と、レースごとの値（区間推定用）。"""
    p = np.clip(p, EPS, 1 - EPS)
    per_row = -(y * np.log(p) + (1 - y) * np.log(1 - p))
    by = defaultdict(list)
    for r, v in zip(race, per_row):
        by[r].append(v)
    per_race = np.array([np.mean(v) for v in by.values()])
    return {"mean": float(per_race.mean()), "per_race": per_race,
            "races": list(by.keys())}


def boot_diff(a_per_race: np.ndarray, b_per_race: np.ndarray,
              n: int = 4000, seed: int = 0) -> tuple[float, float]:
    """a − b の95%区間。**同じレースを対で**再抽出する。"""
    assert len(a_per_race) == len(b_per_race)
    rng = np.random.default_rng(seed)
    k = len(a_per_race)
    out = np.empty(n)
    for i in range(n):
        idx = rng.integers(0, k, k)
        out[i] = a_per_race[idx].mean() - b_per_race[idx].mean()
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


# ── 学習 ──────────────────────────────────────────
# 期間の切り方。**3つは1日も重ねない。**
#   訓練   土台モデル。A/D/E はオッズ不要なので1月から使える
#   校正   E の後段だけがここを使う。土台の訓練にも検証にも入れない
#   検証   ここだけで判定する
TRAIN_FROM = "2026-01-01"
# (訓練終わり, 校正の期間, 検証の期間)。**互いに1日も重ねない。**
# 2つ回すのは堅牢性のため。片方だけだと期間固有の偶然と区別できない。
SPLITS = [
    ("2026-06-30", ("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-09-03")),
    ("2026-05-31", ("2026-06-01", "2026-06-30"), ("2026-07-01", "2026-07-31")),
]
# B/C は訓練にもオッズが要るので、5月以降に制限する。
# ⚠️ 比較の相手（A）も同じ期間に制限しないと、市場の効果と
#    訓練データ量の差が混ざる。
BC_TRAIN_FROM = "2026-05-01"


def fit_ranker(df: pd.DataFrame, feats: list[str], seed: int = 42):
    """打ち切りまでのデータだけで LambdaRank を訓練する。

    ⚠️ 本番モデル（load_ranker）は**使わない**。使うと本番の訓練期間に
    検証期間が入り込み、その案だけ in-sample になる。2026-08-12 の実験は
    これで A だけが有利になっていた。
    """
    import lightgbm as lgb
    d = df.dropna(subset=["arrival_order"])
    d = d[d["arrival_order"] > 0]
    d = d.sort_values(["race_date", "race_id", "boat_no"]).reset_index(drop=True)
    X = d[feats].apply(pd.to_numeric, errors="coerce").values.astype(float)
    med = np.nanmedian(X, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    X = np.where(np.isnan(X), med, X)
    y = (4 - d["arrival_order"].astype(int).clip(1, 4)).clip(0, 3).values
    groups = d.groupby("race_id", sort=False).size().values
    r = lgb.LGBMRanker(
        objective="lambdarank", n_estimators=500, learning_rate=0.05,
        max_depth=6, num_leaves=31, subsample=0.8, colsample_bytree=0.8,
        min_child_samples=20, random_state=seed, n_jobs=-1, verbose=-1,
        label_gain=[0, 1, 3, 7])
    r.fit(X, y, group=groups)
    r._medians = med
    r._feats = feats
    return r, d["race_id"].nunique()


def combo_probs(ranker, df: pd.DataFrame, mkt: dict, temp: float = 1.0) -> pd.DataFrame:
    """レースごとに 2連複15通りの (モデル確率, 市場確率, 的中) を作る。"""
    X = df[ranker._feats].apply(pd.to_numeric, errors="coerce").values.astype(float)
    X = np.where(np.isnan(X), ranker._medians, X)
    d = df.assign(_s=ranker.predict(X))
    with get_engine().connect() as c:
        pay = c.execute(text(
            "SELECT race_id, combination FROM payouts WHERE bet_type = :bt"),
            {"bt": BT}).fetchall()
    won = defaultdict(set)
    for rid, cb in pay:
        won[int(rid)].add(str(cb))

    rows = []
    for rid, g in d.groupby("race_id", sort=False):
        rid = int(rid)
        if rid not in mkt or rid not in won or len(g) != 6:
            continue
        scores = {int(b): float(s) for b, s in zip(g["boat_no"], g["_s"])}
        probs = {c["combination"]: float(c["model_prob"])
                 for c in pl.all_bet_probs(scores, temperature=temp).get(BT, [])}
        if len(probs) != NEED:
            continue
        for cb, pm in probs.items():
            rows.append({"race_id": rid, "cb": cb, "pm": pm,
                         "pk": mkt[rid][cb], "y": 1 if cb in won[rid] else 0})
    return pd.DataFrame(rows)


def add_market_feature(df: pd.DataFrame, mkt: dict) -> pd.DataFrame:
    """艇ごとの市場確率（2着以内）を特徴量として足す。市場が無い行は落とす。"""
    per_race = {rid: boat_market(v) for rid, v in mkt.items()}
    vals = [per_race.get(int(r), {}).get(int(b), np.nan)
            for r, b in zip(df["race_id"], df["boat_no"])]
    out = df.assign(mkt_top2=vals)
    return out.dropna(subset=["mkt_top2"])


def renorm(p: np.ndarray, race: np.ndarray) -> np.ndarray:
    """レース内で合計1に戻す（15通りは互いに排他）。"""
    s = pd.Series(p).groupby(pd.Series(race)).transform("sum").values
    return np.where(s > 0, p / s, p)


def run_split(feat: pd.DataFrame, mkt: dict, train_to: str,
              calib: tuple[str, str], test: tuple[str, str], quick: bool) -> dict:
    CALIB_FROM, CALIB_TO = calib
    TEST_FROM, TEST_TO = test
    print("=" * 74)
    print(f"訓練 {TRAIN_FROM}〜{train_to} / 校正 {CALIB_FROM}〜{CALIB_TO} "
          f"/ 検証 {TEST_FROM}〜{TEST_TO}")
    print("=" * 74)

    def win(d1, d2):
        s = feat["race_date"].astype(str)
        return feat[(s >= d1) & (s <= d2)]

    tr = win(TRAIN_FROM, train_to)
    ca, te = win(CALIB_FROM, CALIB_TO), win(TEST_FROM, TEST_TO)
    print(f"\n行数  訓練 {len(tr):,} / 校正 {len(ca):,} / 検証 {len(te):,}")

    results: dict[str, pd.DataFrame] = {}

    # A: 現行の特徴量だけ
    ra, n = fit_ranker(tr, FEATURE_COLS)
    print(f"  A 現行        訓練 {n:,}レース")
    results["A 現行"] = combo_probs(ra, te, mkt)

    # D: 直前情報を足す（before_info がある期間でしか作れない）
    dcols = FEATURE_COLS + EXTRA_FEATURE_COLS
    have = [c for c in dcols if c in feat.columns]
    if len(have) == len(dcols):
        rd, n = fit_ranker(tr, dcols)
        print(f"  D 直前情報込み 訓練 {n:,}レース")
        results["D 直前情報込み"] = combo_probs(rd, te, mkt)
    else:
        print(f"  D 直前情報込み 列が足りず不可: {set(dcols) - set(have)}")

    # E: 二段階補正。土台は A（市場を見ていない）。後段だけ校正期間で当てる
    from sklearn.linear_model import LogisticRegression
    ca_df = combo_probs(ra, ca, mkt)
    if len(ca_df) >= 1000:
        def lg(x):
            x = np.clip(x, EPS, 1 - EPS)
            return np.log(x / (1 - x))
        base = results["A 現行"]

        def calibrate(cols: list[str], label: str):
            Z = np.column_stack([lg(ca_df[c]) for c in cols])
            cal = LogisticRegression(max_iter=1000).fit(Z, ca_df["y"])
            Zt = np.column_stack([lg(base[c]) for c in cols])
            out = base.copy()
            out["pm"] = renorm(cal.predict_proba(Zt)[:, 1], base["race_id"].values)
            coef = "  ".join(f"{c}={v:+.3f}" for c, v in zip(cols, cal.coef_[0]))
            print(f"  {label:14} 校正 {ca_df['race_id'].nunique():,}レース  {coef}")
            return out

        # ⚠️⚠️ 対照実験。これを出さずに「市場に勝った」と言ってはいけない。
        # 市場は本命-大穴バイアスで**そもそも較正がずれている**（実測で
        # 大穴帯 −0.34pt / 本命寄り +1.64pt）。だから logit(市場) を
        # 較正し直すだけでも生の市場を上回りうる。それはモデルの手柄ではない。
        #   E0  市場だけを較正   ← モデルの寄与ゼロの対照
        #   E1  モデルだけを較正 ← 市場の寄与ゼロの対照
        #   E   両方            ← E > E0 で初めて「モデルが情報を足した」
        results["E0 市場のみ較正"] = calibrate(["pk"], "E0 市場のみ較正")
        results["E1 モデルのみ較正"] = calibrate(["pm"], "E1 モデルのみ較正")
        results["E 二段階補正"] = calibrate(["pm", "pk"], "E 二段階補正")
    else:
        print("  E 二段階補正   校正データ不足")

    # B / C: 訓練にもオッズが要る。A も同じ期間に制限して比べる
    if not quick:
        tr_bc = add_market_feature(win(BC_TRAIN_FROM, train_to), mkt)
        te_bc = add_market_feature(te, mkt)
        bcols = FEATURE_COLS + ["mkt_top2"]
        rb, n = fit_ranker(tr_bc, bcols)
        print(f"  B 市場込み     訓練 {n:,}レース（5月以降に制限）")
        results["B 市場込み"] = combo_probs(rb, te_bc, mkt)
        ra2, n2 = fit_ranker(tr_bc, FEATURE_COLS)
        print(f"  A' 同期間の現行 訓練 {n2:,}レース（B/Cとの比較用）")
        results["A' 同期間"] = combo_probs(ra2, te_bc, mkt)

    # ── 判定 ──
    print("\n" + "=" * 74)
    print(f"{'案':16} {'組合せ':>9} {'レース':>7} {'対数損失':>9} "
          f"{'市場との差':>11} {'95%区間':>18}")
    print("-" * 74)
    for name, df in results.items():
        if df.empty:
            print(f"{name:16} データなし")
            continue
        mm = logloss_by_race(df["pm"].values, df["y"].values, df["race_id"].values)
        mk = logloss_by_race(df["pk"].values, df["y"].values, df["race_id"].values)
        lo, hi = boot_diff(mm["per_race"], mk["per_race"])
        d = mm["mean"] - mk["mean"]
        mark = " ⭐市場に勝ち" if hi < 0 else (" 市場に負け" if lo > 0 else " 差なし")
        print(f"{name:16} {len(df):9,} {df['race_id'].nunique():7,} "
              f"{mm['mean']:9.5f} {d:+11.5f} [{lo:+.5f},{hi:+.5f}]{mark}")
    if results:
        any_df = next(iter(results.values()))
        mk = logloss_by_race(any_df["pk"].values, any_df["y"].values,
                             any_df["race_id"].values)
        print(f"{'（市場そのもの）':16} {'':9} {'':7} {mk['mean']:9.5f}")

    # ⚠️ 本命の検算: E が E0（市場のみ較正）を上回るか。
    # 上回らなければ「市場を較正し直しただけ」で、モデルの寄与はゼロ。
    if "E 二段階補正" in results and "E0 市場のみ較正" in results:
        e, e0 = results["E 二段階補正"], results["E0 市場のみ較正"]
        le = logloss_by_race(e["pm"].values, e["y"].values, e["race_id"].values)
        l0 = logloss_by_race(e0["pm"].values, e0["y"].values, e0["race_id"].values)
        lo, hi = boot_diff(le["per_race"], l0["per_race"])
        d = le["mean"] - l0["mean"]
        print(f"\n⭐ E − E0（モデルが市場に足した分）= {d:+.5f} "
              f"[{lo:+.5f},{hi:+.5f}]")
        print("   " + ("モデルは市場を超える情報を足している" if hi < 0
                       else "**モデルの寄与は無い。市場を較正し直しただけ**"
                       if lo > 0 else "差なし＝モデルの寄与は確認できない"))
    return results


def fukusho_check(results: dict) -> None:
    """⭐ 事前登録どおり、複勝の最上位帯だけに絞って別に出す。

    実質控除率が約2%の唯一の区域なので、全体で負けていても
    ここで勝てていれば意味がある（逆もある）。
    2連複の組合せ確率から艇ごとの「2着以内」＝複勝の確率を作り直して測る。
    """
    print("\n" + "=" * 74)
    print("複勝（＝2着以内）の最上位帯だけに絞った対数損失")
    print("=" * 74)
    print(f"{'案':16} {'艇数':>8} {'対数損失':>9} {'市場との差':>11} {'95%区間':>18}")
    for name, df in results.items():
        if df.empty or name.startswith("A'"):
            continue
        # 組合せ → 艇ごとに畳む
        rows = defaultdict(lambda: [0.0, 0.0, 0])
        for rid, cb, pm, pk, y in zip(df["race_id"], df["cb"], df["pm"],
                                      df["pk"], df["y"]):
            a, b = (int(x) for x in cb.split("-"))
            for bo in (a, b):
                r = rows[(rid, bo)]
                r[0] += pm
                r[1] += pk
                r[2] += y
        arr = np.array([[v[0], v[1], min(v[2], 1)] for v in rows.values()])
        rid_arr = np.array([k[0] for k in rows])
        top = arr[:, 0] >= 0.904          # 複勝の最上位帯（未使用データの四分位）
        if top.sum() < 200:
            continue
        s, r_ = arr[top], rid_arr[top]
        mm = logloss_by_race(s[:, 0], s[:, 2], r_)
        mk = logloss_by_race(s[:, 1], s[:, 2], r_)
        lo, hi = boot_diff(mm["per_race"], mk["per_race"])
        d = mm["mean"] - mk["mean"]
        mark = " ⭐勝ち" if hi < 0 else (" 負け" if lo > 0 else " 差なし")
        print(f"{name:16} {int(top.sum()):8,} {mm['mean']:9.5f} "
              f"{d:+11.5f} [{lo:+.5f},{hi:+.5f}]{mark}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="B/C を省いて速く回す")
    a = ap.parse_args()

    last = max(t[1] for _, _, t in SPLITS)
    mkt = load_market(TRAIN_FROM, last, "final")
    feat = build_features(TRAIN_FROM, last, include_target=True)
    feat = feat.dropna(subset=["target_win"])
    print(f"市場確率のあるレース {len(mkt):,} / 特徴量 {len(feat):,}行\n")

    for i, (train_to, calib, test) in enumerate(SPLITS, 1):
        print(f"\n########## 分割 {i}/{len(SPLITS)} ##########")
        res = run_split(feat, mkt, train_to, calib, test, a.quick)
        if i == 1:
            fukusho_check(res)
    print("\n⚠️ 判定は E（主）と D（副・α=0.025）のみ。A/B/C/E0/E1 は参考・対照。")
    print("⚠️ 2つの分割で符号が揃わなければ、期間固有の偶然として扱うこと。")


if __name__ == "__main__":
    main()
