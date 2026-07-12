"""GitHub Actions / ローカルから走らせて Discord (or Slack) に PDCA サマリーを送る。
使い方: python scripts/notify.py [--kind daily_push|morning_check|judge_done] [--date YYYY-MM-DD]
webhook URL は環境変数 NOTIFY_WEBHOOK_URL (Discord/Slack 互換) から読む。
未設定なら stdout に出力するだけ (dry-run 相当)。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "docs" / "data"


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"load fail {path}: {e}", file=sys.stderr)
        return None


def _send(text: str) -> None:
    url = os.environ.get("NOTIFY_WEBHOOK_URL")
    if not url:
        print("[dry-run: NOTIFY_WEBHOOK_URL not set]")
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(text)
        return
    is_discord = "discord.com" in url
    payload = {"content": text} if is_discord else {"text": text}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"notified: HTTP {resp.status}")
    except Exception as e:
        print(f"webhook error: {e}", file=sys.stderr)
        sys.exit(1)


def _fmt_agg(agg: dict) -> str:
    if not agg or not agg.get("bets"):
        return "0件"
    roi = agg.get("roi")
    return (
        f"{agg['bets']}件 "
        f"hit={agg.get('hits') or 0} "
        f"({(agg.get('hit_rate') or 0)*100:.1f}%) "
        f"ROI={(roi*100 if roi else 0):.0f}% "
        f"P&L={'+' if (agg.get('profit') or 0) >= 0 else ''}{agg.get('profit') or 0}"
    )


def daily_push(target_date: date | None = None) -> str:
    d = target_date or date.today()
    bets_path = DATA_DIR / f"bets_{d}.json"
    pdca = _load_json(DATA_DIR / "pdca.json")
    bets = _load_json(bets_path) or []
    n = len(bets)
    by_bt: dict[str, list] = {}
    for b in bets:
        by_bt.setdefault(b["bet_type"], []).append(b)

    parts = [f"**朝更新 {d}** — {n}件"]
    for bt, lst in sorted(by_bt.items()):
        avg_ev = sum(x.get("expected_value") or 0 for x in lst) / len(lst) if lst else 0
        avg_odds = sum(x.get("odds") or 0 for x in lst) / len(lst) if lst else 0
        parts.append(f"  • {bt}: {len(lst)}件 avg EV={avg_ev:.2f} avg odds={avg_odds:.1f}")

    if pdca:
        w7 = pdca.get("windows", {}).get("7d", {}).get("total", {})
        w30 = pdca.get("windows", {}).get("30d", {}).get("total", {})
        parts.append(f"\n**直近7日**: {_fmt_agg(w7)}")
        parts.append(f"**直近30日**: {_fmt_agg(w30)}")

        # calibration drift 警告
        warns = []
        for r in pdca.get("calibration_recheck", []):
            if r.get("actual_n", 0) >= 20 and r.get("delta_pct") is not None:
                if abs(r["delta_pct"]) >= 50:
                    warns.append(
                        f"  ⚠ {r['bet_type']} raw≤{r['raw_mp_max']}: "
                        f"config {r['config_hit_rate']*100:.1f}% vs 実測 {(r['actual_hit_rate'] or 0)*100:.1f}% "
                        f"(Δ{r['delta_pct']:+.0f}%, n={r['actual_n']})"
                    )
        if warns:
            parts.append("\n**Calibration ドリフト**:")
            parts.extend(warns)
    return "\n".join(parts)


def morning_check(target_date: date | None = None) -> str:
    d = target_date or datetime.now(JST).date()
    bets_path = DATA_DIR / f"bets_{d}.json"
    if bets_path.exists():
        return f"✅ 朝更新 OK: bets_{d}.json 存在"
    return f"❗ 朝更新 未実行: bets_{d}.json が存在しません ({d} {datetime.now(JST).strftime('%H:%M')})"


def judge_done(target_date: date | None = None) -> str:
    d = target_date or datetime.now(JST).date()
    pdca = _load_json(DATA_DIR / "pdca.json") or {}
    daily = pdca.get("daily", [])
    today = next((x for x in daily if x["date"] == str(d)), None)
    parts = [f"**判定完了 {d}**"]
    if not today:
        parts.append("(データなし)")
    else:
        parts.append(_fmt_agg(today.get("total", {})))
        for bt, agg in (today.get("by_bet_type") or {}).items():
            parts.append(f"  • {bt}: {_fmt_agg(agg)}")
    w7 = pdca.get("windows", {}).get("7d", {}).get("total", {})
    parts.append(f"\n直近7日: {_fmt_agg(w7)}")
    return "\n".join(parts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--kind", choices=["daily_push", "morning_check", "judge_done"], required=True)
    p.add_argument("--date", help="YYYY-MM-DD (省略時は今日)")
    args = p.parse_args()
    d = date.fromisoformat(args.date) if args.date else None

    if args.kind == "daily_push":
        text = daily_push(d)
    elif args.kind == "morning_check":
        text = morning_check(d)
    else:
        text = judge_done(d)

    _send(text)


if __name__ == "__main__":
    main()
