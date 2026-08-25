"""検証中の候補ルールの本体。検証も本番もここを呼ぶ。

なぜ1箇所にまとめるか
--------------------
2026-08-24〜25 に、同じ意味の定数を複数ファイルに書いていたせいで2回事故った
（見送り理由の文字列のズレ / 突き合わせキーの取り違え）。ルールの中身は
必ずここだけに置き、検証スクリプトも本番も同じ関数を呼ぶ。

top1_value（2026-08-25 登録）
---------------------------
    ① レースごとに model_prob が最大の2連複を1点だけ選ぶ（**オッズを使わない**）
    ② 板から確定オッズを推定する
         F_hat = exp(a + b*log(板) + c*log(1/p))
    ③ p * F_hat >= min_ev なら記録する（edge >= 2.0 と同値）

なぜこの形か
-----------
ウォークフォワード 14,187レース・2窓（scripts/walkforward.py）で、
**モデルの1点を確率だけで選び、オッズが高いものに絞る**と

    edge 2.0以上: 回収 142.1% [下限120] / 118.2% [下限97]   ★両窓100%超

が出た。これまで試したどの条件よりも母数と再現性がある。

⚠️ ただし上の測定は**確定オッズ**を使っている。買う時点では知り得ないので
板から推定する（②）。板で絞る以上、上振れを拾う圧力は残る。過去2回の候補は
そこで崩れた（[[project_odds_board_vs_final]]）。だから採用せず記録から始める。

①がオッズを使わないのが過去との違い。崩れた2つは買い目の選択自体を EV
（＝オッズ）で行っており、オッズの上振れを狙い撃ちしていた。
"""
from __future__ import annotations

import math

# 2連複の控除後（実測 sum(1/確定オッズ)=1.348）。市場確率 = KEEP / オッズ。
TAKEOUT_KEEP = 0.742


def adjusted_odds(board_odds: float, model_prob: float,
                  a: float, b: float, c: float) -> float:
    """板のオッズから、締切時の確定オッズを見込む。

    板が高いほど、またモデルが本命と見るほど、割り引いて着地する。
    係数の出どころは scripts/estimate_shrink.py（板と確定の実測13,845組）。
    """
    if board_odds <= 0 or model_prob <= 0:
        return 0.0
    return math.exp(a + b * math.log(board_odds) + c * math.log(1.0 / model_prob))


def edge_of(model_prob: float, odds: float) -> float:
    """モデル確率 ÷ 市場の含意確率。1.348 を超えて初めて期待値が1を超える。"""
    if odds <= 0:
        return 0.0
    return model_prob * odds / TAKEOUT_KEEP


def pick_top1(combos: list[dict]) -> dict | None:
    """model_prob が最大の1点を返す。**オッズは見ない。**

    combos: [{"combination": str, "model_prob": float, "odds": float}, ...]
    """
    valid = [c for c in combos if (c.get("model_prob") or 0) > 0]
    return max(valid, key=lambda c: c["model_prob"]) if valid else None


def evaluate(combos: list[dict], cfg: dict) -> dict | None:
    """1レース分の組合せを受けて、記録すべき1点を返す（無ければ None）。

    cfg は config の operation.candidate_rule。
    """
    top = pick_top1(combos)
    if top is None:
        return None
    p = float(top["model_prob"])
    board = float(top.get("odds") or 0)
    if board <= 0:
        return None
    coef = cfg.get("coef") or {}
    adj = adjusted_odds(board, p, float(coef.get("a", 0.2910)),
                        float(coef.get("b", 0.5527)), float(coef.get("c", 0.2899)))
    if adj <= 0:
        return None
    min_ev = float(cfg.get("min_ev", 1.484))
    ev = p * adj
    if ev < min_ev:
        return None
    return {
        "bet_type": top.get("bet_type", "nirenfuku"),
        "combination": top["combination"],
        "model_prob": round(p, 4),
        "odds": board,                 # 見えていた板。あとで係数を測り直せるよう残す
        "adj_odds": round(adj, 2),
        "expected_value": round(ev, 4),
        "edge": round(edge_of(p, adj), 3),
    }
