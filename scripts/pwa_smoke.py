"""PWA を実際のブラウザで描画させ、意図どおり出ているかを見る。

なぜ要るか
----------
2026-08-30 に賭式を6つへ広げたが、画面には 2連複しか出ていなかった。
生成側（bets JSON）には 822本・6賭式すべて入っていたので、Python 側の
テストは全部通っていた。落ちていたのは `docs/js/app.js` の1行:

    state._betsCache = all.filter(b => !isCandidate(b));   // 賭け金0を全部落とす

**JS を一度も実行していなかったから気づけなかった。** ここでブラウザに
描かせて、カードの数と賭式の種類を数える。

使い方
------
    python scripts/pwa_smoke.py                 # 今日
    python scripts/pwa_smoke.py 2026-08-31      # 日付指定
    python scripts/pwa_smoke.py --shot out.png  # 画面も保存

終了コード 0=OK / 1=NG。NG の内容は標準出力に出す。
"""
from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import socket
import socketserver
import sys
import threading
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve(directory: Path):
    """docs/ を http で出す。

    file:// では fetch が CORS で落ちるので、必ず http で読ませる。
    """
    port = _free_port()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

        def log_message(self, *a):       # アクセスログは黙らせる
            pass

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            httpd.shutdown()


def expected_from_json(d: str) -> dict:
    """bets JSON から「画面にこう出るはず」を計算する。

    画面の数字と突き合わせる相手。ここを app.js と同じロジックで書くと
    両方同時に間違えるので、**JSON の素の中身から**素直に数える。
    """
    p = DOCS / "data" / f"bets_{d}.json"
    if not p.exists():
        return {}
    bets = json.loads(p.read_text(encoding="utf-8"))
    trial = {"market_blend", "shrink_adj", "top1_value"}
    displayable = [b for b in bets if b.get("rule") not in trial]
    purchased = [b for b in bets
                 if (b.get("recommended_amount") or 0) > 0
                 and b.get("rule") not in (trial | {"record"})]
    types = sorted({b.get("bet_type") for b in displayable})
    return {
        "n_all": len(bets),
        "n_displayable": len(displayable),
        "n_purchased": len(purchased),
        "bet_types": types,
        "invested": sum(b.get("recommended_amount") or 0 for b in purchased),
    }


def run(d: str, shot: str | None) -> int:
    from playwright.sync_api import sync_playwright

    exp = expected_from_json(d)
    if not exp:
        print(f"NG  docs/data/bets_{d}.json が無い")
        return 1

    errors: list[str] = []
    with serve(DOCS) as base, sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 420, "height": 900})
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(f"{base}/index.html?d={d}", wait_until="networkidle")
        # 画面の日付を目的の日に合わせる（既定は今日）。
        # 一覧の突き合わせは「すべて」表示で行う（既定は「買うべき」で絞られる）。
        page.evaluate(
            "d => { state.date = d; state.betsShowAll = true;"
            "       state.betsView = 'all'; loadBets(); }", d)
        page.wait_for_function(
            "() => { const e = document.getElementById('bet-list');"
            "  return e && !e.textContent.includes('読込中'); }", timeout=20000)
        page.wait_for_timeout(600)

        # 「買うべき」表示の中身も見る。ここが 0 だと画面が空になる。
        buy = page.evaluate("""() => {
            state.betsView = 'buy'; state.betsShowAll = true; renderBets();
            const cards = [...document.querySelectorAll('#bet-list .bet-card')];
            const r = {
              n: cards.length,
              types: [...new Set([...document.querySelectorAll(
                '#bet-list .bet-type-label')].map(e => e.textContent.trim()))].sort(),
              // 賭け金が付いた買い目が絞り込みで消えていないか
              paid: (state._listCache || []).filter(isPurchased).length,
              paidShown: (state._listCache || [])
                .filter(b => isPurchased(b) && isRecommended(b)).length,
            };
            state.betsView = 'all'; renderBets();
            return r;
        }""")

        got = page.evaluate("""() => ({
            cards: document.querySelectorAll('#bet-list .bet-card').length,
            types: [...new Set([...document.querySelectorAll(
                     '#bet-list .bet-type-label')].map(e => e.textContent.trim()))].sort(),
            chips: [...document.querySelectorAll('#bets-filter-area .filter-bar')]
                     .map(bar => [...bar.querySelectorAll('.filter-chip')]
                       .map(c => c.textContent.trim().replace(/\\s+/g, ' '))),
            summary: (document.querySelector('.bets-summary') || {}).textContent || '',
            listCache: (state._listCache || []).length,
            betsCache: (state._betsCache || []).length,
            empty: (document.querySelector('#bet-list .empty') || {}).textContent || '',
        })""")

        if shot:
            page.screenshot(path=shot, full_page=False)
        browser.close()

    JP = {"tansho": "単勝", "fukusho": "複勝", "kakurenfuku": "拡連複",
          "nirentan": "2連単", "nirenfuku": "2連複",
          "sanrenfuku": "3連複", "sanrentan": "3連単"}
    want_types = sorted(JP.get(t, t) for t in exp["bet_types"])

    ng: list[str] = []
    if got["empty"]:
        ng.append(f"一覧が空: {got['empty']!r}")
    if got["listCache"] != exp["n_displayable"]:
        ng.append(f"一覧の元データ {got['listCache']} 件 "
                  f"（JSON から数えると {exp['n_displayable']} 件）")
    if got["betsCache"] != exp["n_purchased"]:
        ng.append(f"成績用 {got['betsCache']} 件 "
                  f"（買った買い目は {exp['n_purchased']} 件）")
    if got["types"] != want_types:
        ng.append(f"画面に出た賭式 {got['types']} / 出るはず {want_types}")
    if got["cards"] != exp["n_displayable"]:
        ng.append(f"カード {got['cards']} 枚 / {exp['n_displayable']} 枚")
    # ⚠️ 賭け金が付いた買い目が「買うべき」から漏れてはいけない。
    # 自分が買った買い目が絞り込みで消えるのが一番まずい。
    if buy["paid"] != buy["paidShown"]:
        ng.append(f"賭け金つきの買い目が「買うべき」から漏れている: "
                  f"{buy['paid'] - buy['paidShown']}本")
    if exp["n_displayable"] >= 100 and buy["n"] == 0:
        ng.append("「買うべき」が0件（画面が空になる）")
    # ⚠️⚠️ 既定ビューに**すべての賭式**が出ること。
    # 2026-09-03 まで、ここを「0件でないこと」しか見ておらず、
    # 拡連複・3連複・3連単が既定ビューから**丸ごと消えたまま4日間通っていた**。
    # 原因は絶対基準（的中率>=0.70）で、各賭式の上限が
    # 複勝89% 単勝76% 拡連複68% 2連複43% 3連複35% 3連単15% なので
    # 下4つは永久に届かない。「すべて」ビューでは出ていたので件数照合も通った。
    if got["types"] and buy["types"] != want_types:
        missing = [t for t in want_types if t not in buy["types"]]
        ng.append(f"既定ビューに出ない賭式 {missing}"
                  f"（既定 {buy['types']} / 出るはず {want_types}）")
    for e in errors:
        ng.append(e)

    print(f"日付        {d}")
    print(f"JSON        全{exp['n_all']}本 → 表示{exp['n_displayable']} / "
          f"買う{exp['n_purchased']} / 投資¥{exp['invested']:,}")
    print(f"カード      {got['cards']} 枚（すべて表示）")
    print(f"買うべき    {buy['n']} 枚  {' '.join(buy['types']) or '(なし)'}"
          f"  ／ 賭け金つき {buy['paidShown']}/{buy['paid']} 本を含む")
    print(f"賭式        {' '.join(got['types']) or '(なし)'}")
    for i, bar in enumerate(got["chips"]):
        print(f"チップ{i + 1}     {' | '.join(bar[:9])}"
              + (" …" if len(bar) > 9 else ""))
    print(f"サマリー    {got['summary'].strip()}")
    if shot:
        print(f"画面        {shot}")

    if ng:
        print("\nNG")
        for m in ng:
            print("  -", m)
        return 1
    print("\nOK  画面は JSON のとおり出ている")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=date.today().isoformat())
    ap.add_argument("--shot", default=None, help="スクリーンショットの保存先")
    a = ap.parse_args()
    sys.exit(run(a.date, a.shot))
