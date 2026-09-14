#!/usr/bin/env python3
"""generate_launcher_icons.py — 从 design/app_icon_source.png 生成全套启动器图标。

源图是"圆角方形满铺图标截图放在黑色画布上"：外圈有黑底与青色描边伪影。
自适应图标双层结构（主体须留边距——满铺会在启动器遮罩下显得过大，2026-09-14 用户反馈）：

  1. 自适应检测并裁掉外圈伪影（四角/四边 8 条路径向内扫，全部离开伪影色再加 1% 余量）；
  2. foreground = 主体缩放到 CONTENT_SCALE 居中，外圈 FEATHER 比例羽化淡出到透明；
  3. background = 主体边缘拉伸铺满画布 + 强高斯模糊：边界颜色与内容边缘一致
     （无方形光晕），模糊后无拉伸条纹与内容残影；
  4. 产出 mipmap-anydpi-v26 自适应图标 XML + 各密度 foreground/background PNG；
  5. 兜底各密度 ic_launcher.png（圆角方）/ ic_launcher_round.png（圆形）合成位图，
     服务仍直接读 mipmap 位图的场景（设置、分享面板等）；
  6. 输出 design/icon_preview.png 预览拼图，含实际渲染尺寸（144/72/48px）供人工核对。
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

FRONTEND_DIR = Path(__file__).resolve().parent.parent
MODULE_DIR = FRONTEND_DIR / "Front"
RES_DIR = MODULE_DIR / "app" / "src" / "main" / "res"
SRC = MODULE_DIR / "design" / "app_icon_source.png"
PREVIEW = MODULE_DIR / "design" / "icon_preview.png"

EDGE_TRIM = None  # 自适应检测伪影宽度；设为像素数时强制裁剪
CONTENT_SCALE = 0.64  # 主体占画布比例：≤0.667 保证圆角方遮罩下四周仍有边距
FEATHER = 0.05  # 前景边缘羽化比例（外圈 5% 线性淡出到透明）
CANVAS = 1080  # 自适应图标画布工作分辨率（108dp 的 10 倍）

DENSITIES = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}
LAUNCHER_DP = 48  # 传统启动器图标边长（dp）
FOREGROUND_DP = 108  # 自适应图标画布（dp）

ADAPTIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@mipmap/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
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
    """画面四边 1px 的均色（调试参考值）。"""
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


def edge_fade_alpha(size: int, feather: int) -> Image.Image:
    """四边线性羽化 alpha 蒙版：外圈 feather 像素由 0 渐变到 255。"""

    def ramp_1d() -> Image.Image:
        row = Image.new("L", (size, 1), 255)
        px = row.load()
        for x in range(feather):
            v = round(255 * x / feather)
            px[x, 0] = v
            px[size - 1 - x, 0] = v
        return row

    horizontal = ImageChops.darker(Image.new("L", (size, 1), 255), ramp_1d())
    alpha = horizontal.resize((size, size))
    vertical = ramp_1d().rotate(90, expand=True).resize((size, size))
    return ImageChops.darker(alpha, vertical)


def build_background(art: Image.Image) -> Image.Image:
    """背景 = 主体边缘拉伸铺满画布 + 强高斯模糊。

    拉伸保证背景在内容边界处的颜色等于内容边缘色（无方形光晕），
    强模糊抹平拉伸条纹与内容残影（无色痕、无鬼影）。
    """
    side = round(CANVAS * CONTENT_SCALE)
    ox = (CANVAS - side) // 2
    band = CANVAS - ox - side
    scaled = art.resize((side, side), Image.LANCZOS)

    canvas = Image.new("RGB", (CANVAS, CANVAS))
    canvas.paste(scaled, (ox, ox))
    canvas.paste(scaled.crop((0, 0, side, 1)).resize((CANVAS, ox), Image.LANCZOS), (0, 0))
    canvas.paste(
        scaled.crop((0, side - 1, side, side)).resize((CANVAS, band), Image.LANCZOS),
        (0, ox + side),
    )
    canvas.paste(scaled.crop((0, 0, 1, side)).resize((ox, side), Image.LANCZOS), (0, ox))
    canvas.paste(
        scaled.crop((side - 1, 0, side, side)).resize((band, side), Image.LANCZOS),
        (ox + side, ox),
    )
    return canvas.filter(ImageFilter.GaussianBlur(CANVAS * 0.06)).convert("RGBA")


def build_layers(art: Image.Image) -> tuple[Image.Image, Image.Image, Image.Image]:
    """返回 (前景 RGBA, 背景 RGBA, 合成 RGB)。"""
    background = build_background(art)

    side = round(CANVAS * CONTENT_SCALE)
    feather = max(round(side * FEATHER), 1)
    sharp = art.resize((side, side), Image.LANCZOS).convert("RGBA")
    sharp.putalpha(edge_fade_alpha(side, feather))

    foreground = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    foreground.paste(sharp, ((CANVAS - side) // 2,) * 2, sharp)

    composite = Image.alpha_composite(background, foreground).convert("RGB")
    return foreground, background, composite


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
    foreground, background, composite = build_layers(art)

    anydpi = RES_DIR / "mipmap-anydpi-v26"
    anydpi.mkdir(parents=True, exist_ok=True)
    (anydpi / "ic_launcher.xml").write_text(ADAPTIVE_XML, encoding="utf-8")
    (anydpi / "ic_launcher_round.xml").write_text(ADAPTIVE_XML, encoding="utf-8")
    # 历史产物清理：背景层曾用过 values 纯色与 drawable 渐变两个方案
    (RES_DIR / "values" / "ic_launcher_background.xml").unlink(missing_ok=True)
    (RES_DIR / "drawable" / "ic_launcher_background.xml").unlink(missing_ok=True)

    square = masked(composite, "square")
    circle = masked(composite, "circle")
    for name, density in DENSITIES.items():
        folder = RES_DIR / f"mipmap-{name}"
        folder.mkdir(parents=True, exist_ok=True)
        fg = round(FOREGROUND_DP * density)
        background.resize((fg, fg), Image.LANCZOS).save(folder / "ic_launcher_background.png")
        foreground.resize((fg, fg), Image.LANCZOS).save(folder / "ic_launcher_foreground.png")
        legacy = round(LAUNCHER_DP * density)
        square.resize((legacy, legacy), Image.LANCZOS).save(folder / "ic_launcher.png")
        circle.resize((legacy, legacy), Image.LANCZOS).save(folder / "ic_launcher_round.png")

    # 预览拼图：300px 细节（方形/圆形）+ 实际渲染尺寸（144/72/48px 圆形）+ 圆角方
    cell = 300
    scaled = composite.resize((cell, cell), Image.LANCZOS)
    preview = Image.new("RGB", (cell * 4, cell), (235, 235, 235))
    preview.paste(scaled, (0, 0))
    im = masked(scaled, "circle")
    preview.paste(im, (cell, 0), im)
    x = cell * 2 + 10
    for size in (144, 72, 48):
        im = circle.resize((size, size), Image.LANCZOS)
        preview.paste(im, (x, (cell - size) // 2), im)
        x += size + 14
    im = masked(scaled, "square", 0.2)
    preview.paste(im, (cell * 3, 0), im)
    preview.save(PREVIEW)
    print(f"OK: icons -> {RES_DIR}\n    background {edge_color(art)}\n    preview -> {PREVIEW}")


if __name__ == "__main__":
    main()
