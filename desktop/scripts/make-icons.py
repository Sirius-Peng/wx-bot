#!/usr/bin/env python3
"""生成桌面应用图标 —— 应用图标、托盘图标（macOS 模板图）、打包用 icns/ico。

用法：
    python3 scripts/make-icons.py

产出（desktop/assets/）：
    icon.png            1024×1024  应用图标（打包时由 electron-builder 转 icns/ico）
    icon.icns           macOS 图标（本机为 mac 时生成）
    icon.ico            Windows 图标
    tray.png            32×32      托盘图标（Windows / Linux）
    trayTemplate.png    36×36      托盘图标（macOS 模板图，纯黑+透明）
    tray@2x.png         72×72      macOS 高分屏托盘图
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("需要 Pillow：pip install pillow")

ASSETS = Path(__file__).resolve().parent.parent / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

# 微信绿
GREEN_TOP = (10, 200, 105)
GREEN_BOTTOM = (4, 160, 78)
WHITE = (255, 255, 255, 255)


def rounded_mask(size: int, radius_ratio: float = 0.235) -> Image.Image:
    """生成圆角矩形遮罩（macOS 图标风格）。"""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    r = int(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    return mask


def vertical_gradient(size: int, top, bottom) -> Image.Image:
    """垂直渐变背景。"""
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel((0, y), (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        ))
    return grad.resize((size, size), Image.BILINEAR)


def draw_bubble(d: ImageDraw.ImageDraw, cx: float, cy: float, w: float, h: float,
                radius: float, color, tail=True):
    """画一个对话气泡。"""
    x0, y0 = cx - w / 2, cy - h / 2
    x1, y1 = cx + w / 2, cy + h / 2
    d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=color)
    if tail:
        # 左下角小尾巴
        tx = x0 + w * 0.24
        ty = y1 - radius * 0.15
        d.polygon([
            (tx, ty),
            (tx + w * 0.16, ty),
            (tx + w * 0.02, ty + h * 0.30),
        ], fill=color)


def make_app_icon(size: int = 1024) -> Image.Image:
    """应用图标：绿底 + 双气泡 + AI 星芒。"""
    base = vertical_gradient(size, GREEN_TOP, GREEN_BOTTOM).convert("RGBA")
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # 主气泡（大）
    draw_bubble(d, size * 0.455, size * 0.455, size * 0.50, size * 0.42,
                size * 0.105, WHITE)
    # 副气泡（小，右下，半透明）
    draw_bubble(d, size * 0.665, size * 0.655, size * 0.30, size * 0.245,
                size * 0.065, (255, 255, 255, 205))

    # 主气泡里的三个点（"正在输入"）
    for i, fx in enumerate((0.355, 0.455, 0.555)):
        r = size * 0.028
        d.ellipse([size * fx - r, size * 0.445 - r, size * fx + r, size * 0.445 + r],
                  fill=(9, 170, 92, 235))

    # 右下角 AI 星芒（四角星）
    sx, sy, s = size * 0.775, size * 0.775, size * 0.115
    d.polygon([
        (sx, sy - s), (sx + s * 0.26, sy - s * 0.26),
        (sx + s, sy), (sx + s * 0.26, sy + s * 0.26),
        (sx, sy + s), (sx - s * 0.26, sy + s * 0.26),
        (sx - s, sy), (sx - s * 0.26, sy - s * 0.26),
    ], fill=WHITE)

    out = Image.alpha_composite(base, layer)
    out.putalpha(rounded_mask(size))
    return out


def make_tray_icon(size: int, template: bool = False) -> Image.Image:
    """托盘图标。

    template=True 时输出 macOS 模板图：纯黑 + alpha，系统自动适配深浅色菜单栏。
    """
    # 超采样后缩小，边缘更平滑
    ss = size * 4
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    color = (0, 0, 0, 255) if template else (7, 193, 96, 255)

    # 主气泡
    draw_bubble(d, ss * 0.44, ss * 0.44, ss * 0.62, ss * 0.52, ss * 0.15, color)
    # 副气泡
    draw_bubble(d, ss * 0.70, ss * 0.68, ss * 0.34, ss * 0.28, ss * 0.09,
                (0, 0, 0, 190) if template else (7, 193, 96, 200))

    return img.resize((size, size), Image.LANCZOS)


def make_ico(png_path: Path, ico_path: Path):
    """生成 Windows .ico（多尺寸）。"""
    img = Image.open(png_path)
    img.save(ico_path, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ico_path


def make_icns(png_path: Path, icns_path: Path) -> bool:
    """用 macOS 自带 iconutil 生成 .icns。非 macOS 直接跳过。"""
    iconset = ASSETS / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    sizes = [16, 32, 64, 128, 256, 512]
    img = Image.open(png_path)
    for s in sizes:
        img.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
        img.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
    try:
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)],
                       check=True, capture_output=True)
        ok = True
    except (FileNotFoundError, subprocess.CalledProcessError):
        ok = False
    finally:
        # 清理临时 iconset；清理失败不影响结果（某些沙箱会拦截删除）
        try:
            for f in iconset.glob("*"):
                f.unlink()
            iconset.rmdir()
        except OSError:
            pass
    return ok


def main():
    print("生成图标…")

    icon = make_app_icon(1024)
    icon.save(ASSETS / "icon.png")
    print("  ✓ icon.png (1024×1024)")

    make_tray_icon(32).save(ASSETS / "tray.png")
    print("  ✓ tray.png (32×32)")

    make_tray_icon(36, template=True).save(ASSETS / "trayTemplate.png")
    make_tray_icon(72, template=True).save(ASSETS / "trayTemplate@2x.png")
    print("  ✓ trayTemplate.png / @2x（macOS 菜单栏模板图）")

    make_ico(ASSETS / "icon.png", ASSETS / "icon.ico")
    print("  ✓ icon.ico（Windows 多尺寸）")

    if sys.platform == "darwin":
        if make_icns(ASSETS / "icon.png", ASSETS / "icon.icns"):
            print("  ✓ icon.icns（macOS）")
        else:
            print("  · icon.icns 跳过（iconutil 不可用）")
    else:
        print("  · icon.icns 跳过（非 macOS）")

    print(f"\n输出目录：{ASSETS}")


if __name__ == "__main__":
    main()
