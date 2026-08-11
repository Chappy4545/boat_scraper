"""PWA用アイコンを生成する。

docs/icons/ が空で manifest の参照先も存在しなかったため
（ホーム画面に追加しても正しく動かない状態だった）、
アプリのテーマカラーに合わせたアイコンを生成する。

実行: python scripts/make_icons.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "docs" / "icons"
BG = (10, 22, 40)        # #0a1628  manifest の background_color
ACCENT = (33, 118, 214)  # #2176d6  波・船体
WHITE = (240, 246, 255)


def draw_icon(size: int, maskable: bool = False) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG + (255,))
    d = ImageDraw.Draw(img)
    s = size / 512.0  # 512基準で設計

    # maskable は端が切られるので中身を小さめに寄せる（安全領域80%）
    scale = 0.78 if maskable else 1.0
    cx, cy = size / 2, size / 2

    def P(x, y):
        return (cx + (x - 256) * s * scale, cy + (y - 256) * s * scale)

    # 波 3本
    for i, (yy, alpha) in enumerate([(360, 255), (410, 170), (455, 90)]):
        pts = []
        for x in range(60, 456, 8):
            import math
            y = yy + math.sin((x / 396) * math.pi * 3 + i) * 14
            pts.append(P(x, y))
        d.line(pts, fill=ACCENT + (alpha,), width=max(2, int(14 * s * scale)), joint="curve")

    # 船体（シンプルなボート形）
    hull = [P(120, 300), P(392, 300), P(330, 372), P(182, 372)]
    d.polygon(hull, fill=WHITE + (255,))

    # 帆・数字の代わりのマスト
    d.polygon([P(256, 120), P(256, 292), P(350, 292)], fill=ACCENT + (255,))
    d.line([P(256, 110), P(256, 300)], fill=WHITE + (255,), width=max(2, int(12 * s * scale)))

    return img.convert("RGB")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for size in (192, 512):
        p = OUT / f"icon-{size}.png"
        draw_icon(size).save(p, "PNG")
        made.append((p.name, p.stat().st_size))
    # Android のアダプティブアイコン用
    p = OUT / "icon-maskable-512.png"
    draw_icon(512, maskable=True).save(p, "PNG")
    made.append((p.name, p.stat().st_size))
    # ブラウザタブ用
    p = OUT / "favicon-32.png"
    draw_icon(32).save(p, "PNG")
    made.append((p.name, p.stat().st_size))

    for name, sz in made:
        print(f"  {name:26} {sz:>7,} bytes")
    print(f"生成先: {OUT}")


if __name__ == "__main__":
    main()
