"""市場からどれだけ離れて賭けるのが最良か。1つのパラメータで測る。

⚠️ 事前登録（2026-09-03・**結果を1つも見る前に書いてコミットする**）
==================================================================

なぜこの問いなのか
------------------
同日の `market_trial.py` で、**精度が上がるほど回収率が下がる**ことが
両期間で再現した（[[project_two_stage_beats_market]]）:

    市場そのもの   対数損失 最良に近い   回収率 76.0% / 75.2%   ← 最も儲からない
    E 二段階補正   対数損失 **最良**     回収率 78.6% / 76.8%
    A 現行        対数損失 最悪         回収率 **81.9% / 81.9%** ← 最良

パリミュチュエルでは「正確になる＝群衆と一致する＝群衆が買い込んだ
低配当の券を選ぶ」。つまり賭けられる材料は**市場との食い違い**だけ。

この3点は「市場からの距離」で1本に並ぶ。幾何ブレンドで書くと:

    p(α) ∝ p_model^α × p_market^(1−α)     レース内で正規化

    α=0     市場そのもの        回収率 76.0%
    α≈0.27  E（実測の係数比）    回収率 78.6%
    α=1     現行モデル A        回収率 81.9%
    α→∞    食い違い最大＝EV選択  回収率 54.1%（測定済み・別途）

**0→1 で単調に上がり、∞ で崩壊する。ならば頂点は α>1 のどこかにある。**
これが仮説。α は「市場からどれだけ離れて賭けるか」の1つの目盛り。

⚠️ この形は**新しい情報を足していない**。A と市場の組み合わせ方を
変えているだけ。だから「モデルを賢くする」話ではなく
「持っている食い違いをどう使うか」の話。

比べるもの
----------
    α ∈ {0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0}
    各 α で「p(α) が最大の1点」を買ったときの回収率

    ⚠️ 選択に**オッズを直接使わない**。p_market はオッズ由来だが、
       これは「群衆の見立て」として使っているのであって、
       払戻の大きさで選んでいるのではない。EV選択（＝確定オッズで選ぶ）は
       買う時点で知り得ない値を使うので別物。

判定（先に決める）
------------------
    主判定  校正期間で回収率が最大になる α*（α=1 以外）を選び、
            **検証期間**で α* の回収率が α=1（現行）を上回るか。
            レース単位ブートストラップの95%区間が0を含まないこと。
    ⚠️ α* は必ず**校正期間だけ**で選ぶ。検証期間を見て選んだら
       ただの後知恵になる（optimizer's curse）。
    ⚠️ 2つの分割で符号が揃わなければ、期間固有の偶然として扱う。

    参考   α ごとの回収率・的中率の曲線を両期間で出す（形を見るため）
    参考   1号艇が絡む買い目とそれ以外で分けた曲線
           （本命-大穴バイアスは1号艇に集中しているはずなので）

停止条件
--------
α* が α=1 を上回らなければ、**この方向は打ち切る**と明記して報告する。
曲線が綺麗でも「惜しい」を理由に条件を足さない。

⚠️ 仮に上回っても、100%を超えなければ黒字ではない。
   現行 81.9% から損益分岐までは 18pt ある。

━━━ 以下は実行後（2026-09-03）の追記 ━━━
⚠️ 上の事前登録部分は結果を見る前に書いてコミット済み（`7f0e894`）。

結果: **打ち切り。α>1 の利得は確定オッズのオラクルだった。**

主判定は分割1で通らなかった（+7.2pt [-1.9,+16.6]、区間が0を含む）。
分割2は通った（+13.0pt [+3.8,+23.9]）。符号は揃うが有意ではない。

そして**なぜ**かが分かった。α を上げると選ばれる券のオッズが跳ね上がる:

    α=0 中央値2.40倍 → α=1 2.61 → α=3 4.78 → α=5 7.24倍
                                        （90%点は 3.67 → 32.11倍）

つまり α>1 は**確定オッズで高配当の券を選んでいる**。買う時点では
知り得ない値。板（締切間際に保存した実際に見えた値）で選び直すと消える:

    α      確定で選ぶ(オラクル)   板で選ぶ(正直)
    0.00        73.9%          76.3%
    1.00        84.9%          84.9%     ← 現行。ここまでは一致する
    2.00        96.2%          86.3%
    5.00        99.5%          84.8%     ← オラクル99.5%が正直だと84.8%

α=1 まではオッズを選択に使わないので両者は一致し、α>1 で乖離する。
**乖離の全部がオラクル。** 正直な曲線は α=0.75〜5 でほぼ横ばい
（77〜86%、907レースで誤差±7pt）。

生き残ったこと: **α=0（市場）→ α=1（モデル）は本物**。
76.3% → 84.9% で、これは選択にオッズを使っていない。
「モデルの食い違いは市場より良い選び手」は板でも成立する。
ただし 84.9% で損益分岐には 15pt 足りない。

→ [[project_backtest_leak]]（確定オッズで選ぶ罠。何度も刺されている）

使い方
------
    python scripts/tilt_trial.py
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from src.features.builder import FEATURE_COLS, build_features  # noqa: E402
from src.ingestion.database import get_engine  # noqa: E402

import scripts.market_trial as M  # noqa: E402

ALPHAS = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0]
SPLITS = M.SPLITS
EPS = 1e-12


def tilt(pm: np.ndarray, pk: np.ndarray, race: np.ndarray, a: float) -> np.ndarray:
    """p ∝ pm^a * pk^(1-a)。対数で計算してレース内で正規化する。"""
    lg = a * np.log(np.clip(pm, EPS, 1)) + (1 - a) * np.log(np.clip(pk, EPS, 1))
    s = pd.Series(lg)
    # レースごとに最大を引いてから exp（桁あふれ防止）
    lg = (s - s.groupby(pd.Series(race)).transform("max")).values
    p = np.exp(lg)
    tot = pd.Series(p).groupby(pd.Series(race)).transform("sum").values
    return p / tot


def roi_at(df: pd.DataFrame, a: float) -> tuple[np.ndarray, float, float]:
    """α のとき「確率最大の1点」を買った結果。(レースごとの払戻, 回収率, 的中率)"""
    p = tilt(df["pm"].values, df["pk"].values, df["race_id"].values, a)
    d = df.assign(_p=p)
    s = d.loc[d.groupby("race_id")["_p"].idxmax()]
    return s["ret"].values, float(s["ret"].mean()), float(s["y"].mean())


def boot_pair(x: np.ndarray, y: np.ndarray, n=4000, seed=0) -> tuple[float, float]:
    """x − y の95%区間。**同じレースを対で**再抽出する。"""
    rng = np.random.default_rng(seed)
    k = len(x)
    out = np.empty(n)
    for i in range(n):
        idx = rng.integers(0, k, k)
        out[i] = x[idx].mean() - y[idx].mean()
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def with_returns(df: pd.DataFrame, pay: dict) -> pd.DataFrame:
    return df.assign(ret=[pay.get((r, cb), 0.0) / 100.0 if y else 0.0
                          for r, cb, y in zip(df.race_id, df.cb, df.y)])


def main() -> None:
    print(__doc__.split("使い方")[0])
    mkt = M.load_market("2026-01-01", "2026-09-03", "final")
    feat = build_features("2026-01-01", "2026-09-03",
                          include_target=True).dropna(subset=["target_win"])
    with get_engine().connect() as c:
        pay = {(int(r), str(cb)): float(p) for r, cb, p in c.execute(text(
            "SELECT race_id, combination, payout FROM payouts "
            "WHERE bet_type = 'nirenfuku'")).fetchall()}

    def win(d1, d2):
        s = feat["race_date"].astype(str)
        return feat[(s >= d1) & (s <= d2)]

    verdicts = []
    for i, (train_to, ca_w, te_w) in enumerate(SPLITS, 1):
        print(f"\n{'='*70}\n分割{i}  訓練〜{train_to} / 校正 {ca_w[0]}〜{ca_w[1]} "
              f"/ 検証 {te_w[0]}〜{te_w[1]}\n{'='*70}")
        ra, _ = M.fit_ranker(win("2026-01-01", train_to), FEATURE_COLS)
        ca = with_returns(M.combo_probs(ra, win(*ca_w), mkt), pay)
        te = with_returns(M.combo_probs(ra, win(*te_w), mkt), pay)

        print(f"{'α':>6} {'校正 回収率':>11} {'検証 回収率':>11} {'検証 的中率':>11}")
        best_a, best_roi = None, -1.0
        for a in ALPHAS:
            _, r_ca, _ = roi_at(ca, a)
            _, r_te, h_te = roi_at(te, a)
            star = ""
            if a != 1.0 and r_ca > best_roi:
                best_roi, best_a, star = r_ca, a, ""
            print(f"{a:6.2f} {r_ca*100:10.1f}% {r_te*100:10.1f}% {h_te*100:10.1f}%")
        # 主判定: 校正で選んだ α* を検証で現行(α=1)と比べる
        x, rx, _ = roi_at(te, best_a)
        y, ry, _ = roi_at(te, 1.0)
        lo, hi = boot_pair(x, y)
        ok = lo > 0
        verdicts.append((best_a, rx - ry, lo, hi, ok))
        print(f"\n主判定  校正で選んだ α*={best_a}（校正 {best_roi*100:.1f}%）")
        print(f"        検証で α* {rx*100:.1f}% − 現行 {ry*100:.1f}% "
              f"= {(rx-ry)*100:+.1f}pt  [{lo*100:+.1f},{hi*100:+.1f}]")
        print("        " + ("⭐ 現行を上回る" if ok else
                            "上回らない（区間が0を含む）" if hi > 0 else "下回る"))

    print(f"\n{'='*70}")
    if all(v[4] for v in verdicts):
        print("結論: 両分割で現行を上回った。α* =", [v[0] for v in verdicts])
    else:
        print("結論: **この方向は打ち切る。** 両分割で揃って上回ってはいない。")
        print("      α* =", [v[0] for v in verdicts],
              " 差 =", [f"{v[1]*100:+.1f}pt" for v in verdicts])
    print("⚠️ 上回っても100%未満なら黒字ではない。現行81.9%から18pt足りない。")


if __name__ == "__main__":
    main()
