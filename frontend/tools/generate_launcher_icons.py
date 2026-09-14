#!/usr/bin/env python3
"""generate_launcher_icons.py — 从 design/app_icon_source.png 生成全套启动器图标。

源图是"圆角方形满铺图标截图放在黑色画布上"：外圈有黑底与青色描边伪影，
且主体内容（卡片堆、手臂、波浪）本就延伸到画面边缘，无法外延补边。
故前景层直接满铺原画（遮罩外的角落装饰由启动器遮罩自然裁掉，与所有满铺
App 图标一致）。流程（幂等可重跑）：

  1. 自适应检测并裁掉外圈伪影（四角/四边 8 条路径向内扫，全部离开伪影色再加 1% 余量）；
  2. 前景 = 裁边后原画满铺 108dp 画布；背景 = 画面四边均色纯色
     （@color/ic_launcher_background，仅用于遮挡启动器视差间隙）；
  3. 产出 mipmap-anydpi-v26 自适应图标 XML + 各密度 foreground PNG；
  4. 兜底各密度 ic_launcher.png（圆角方）/ ic_launcher_round.png（圆形）合成位图，
     服务仍直接读 mipmap 位图的场景（设置、分享面板等）；
  5. 输出 design/icon_preview.png 预览拼图（方形/圆形/圆角方/实际大小）供人工核对。
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

FRONTEND_DIR = Path(__file__).resolve().parent.parent
MODULE_DIR = FRONTEND_DIR / "Front"
RES_DIR = MODULE_DIR / "app" / "src" / "main" / "res"
SRC = MODULE_DIR / "design" / "app_icon_source.png"
PREVIEW = MODULE_DIR / "design" / "icon_preview.png"

EDGE_TRIM = None  # 自适应检测伪影宽度；设为像素数时强制裁剪
CANVAS = 1080  # 自适应图标画布工作分辨率（108dp 的 10 倍）

DENSITIES = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}
LAUNCHER_DP = 48  # 传统启动器图标边长（dp）
FOREGROUND_DP = 108  # 自适应图标画布（dp）

ADAPTIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
"""

COLOR_XML = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="ic_launcher_background">{color}</color>
</resources>
"""


def detect_trim(im: Image.Image) -> int:
    """自适应检测外圈伪影（黑底/青色描边）宽度。

    四角沿对角线的黑色区域最深，故从四角、四边中点共 8 条路径向内逐像素扫，
    全部离开伪影色后才算穿过杂边，再加 1% 余量。
    """
    w, _ = im.size
    px = im.load()

    def is_artifact(c: tuple[int, int, int]) -> bool:
        r, g, b = c
        return (r + g + b) < 180 or (r < 80 and g > 200 and b > 200)

    probes = (
        lambda t: px[t, t],
        lambda t: px[w - 1 - t, t],
        lambda t: px[t, w - 1 - t],
        lambda t: px[w - 1 - t, w - 1 - t],
        lambda t: px[w // 2, t],
        lambda t: px[w // 2, w - 1 - t],
        lambda t: px[t, w // 2],
        lambda t: px[w - 1 - t, w // 2],
    )
    t = 0
    limit = w // 4
    while t < limit and any(is_artifact(p(t)) for p in probes):
        t += 1
    return min(t + round(w * 0.01), limit)


def trim_source(im: Image.Image) -> Image.Image:
    if im.size[0] != im.size[1]:
        im = ImageOps.fit(im, (min(im.size),) * 2)
    trim = EDGE_TRIM if EDGE_TRIM is not None else detect_trim(im)
    trim = round(trim)
    print(f"    edge trim: {trim}px (source {im.size[0]}px)")
    return im.crop((trim, trim, im.size[0] - trim, im.size[0] - trim))


def edge_color(art: Image.Image) -> str:
    """画面四边 1px 的均色 → 背景层纯色，与前景边缘渐变同族。"""
    w = art.size[0]
    px = art.load()
    edges = (
        [px[x, 0] for x in range(w)]
        + [px[x, w - 1] for x in range(w)]
        + [px[0, y] for y in range(w)]
        + [px[w - 1, y] for y in range(w)]
    )
    avg = tuple(sum(c[i] for c in edges) // len(edges) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(*avg)


def masked(im: Image.Image, shape: str, radius_ratio: float = 0.14) -> Image.Image:
    size = im.size[0]
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    if shape == "circle":
        d.ellipse((0, 0, size - 1, size - 1), fill=255)
    else:
        d.rounded_rectangle(
            (0, 0, size - 1, size - 1), radius=round(size * radius_ratio), fill=255
        )
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


def main() -> None:
    art = trim_source(Image.open(SRC).convert("RGB"))
    color = edge_color(art)
    foreground = art.resize((CANVAS, CANVAS), Image.LANCZOS)

    anydpi = RES_DIR / "mipmap-anydpi-v26"
    anydpi.mkdir(parents=True, exist_ok=True)
    (anydpi / "ic_launcher.xml").write_text(ADAPTIVE_XML, encoding="utf-8")
    (anydpi / "ic_launcher_round.xml").write_text(ADAPTIVE_XML, encoding="utf-8")
    (RES_DIR / "values" / "ic_launcher_background.xml").write_text(
        COLOR_XML.format(color=color), encoding="utf-8"
    )
    # 历史产物清理：背景层曾用过 drawable 渐变与模糊位图两个方案
    (RES_DIR / "drawable" / "ic_launcher_background.xml").unlink(missing_ok=True)
    for name in DENSITIES:
        (RES_DIR / f"mipmap-{name}" / "ic_launcher_background.png").unlink(missing_ok=True)

    square = masked(foreground, "square")
    circle = masked(foreground, "circle")
    for name, density in DENSITIES.items():
        folder = RES_DIR / f"mipmap-{name}"
        folder.mkdir(parents=True, exist_ok=True)
        fg = round(FOREGROUND_DP * density)
        foreground.resize((fg, fg), Image.LANCZOS).save(folder / "ic_launcher_foreground.png")
        legacy = round(LAUNCHER_DP * density)
        square.resize((legacy, legacy), Image.LANCZOS).save(folder / "ic_launcher.png")
        circle.resize((legacy, legacy), Image.LANCZOS).save(folder / "ic_launcher_round.png")

    # 预览拼图：方形 / 圆形遮罩 / 圆角方遮罩 / 实际大小（96px 与 48px，灰底模拟桌面）
    cell = 300
    scaled = foreground.resize((cell, cell), Image.LANCZOS)
    preview = Image.new("RGB", (cell * 4, cell), (235, 235, 235))
    preview.paste(scaled, (0, 0))
    for i, shape in enumerate(("circle", "square"), start=1):
        im = masked(scaled, shape, 0.2)
        preview.paste(im, (cell * i, 0), im)
    for size, dy in ((96, -30), (48, 40)):
        im = circle.resize((size, size), Image.LANCZOS)
        preview.paste(im, (cell * 3 + (cell - size) // 2, (cell - size) // 2 + dy), im)
    preview.save(PREVIEW)
    print(f"OK: icons -> {RES_DIR}\n    background {color}\n    preview -> {PREVIEW}")


if __name__ == "__main__":
    main()
