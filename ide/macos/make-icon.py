#!/usr/bin/env python3
"""Draw the MIT Scheme IDE application icon and write it as .icns and .png.

    python3 make-icon.py [OUTDIR]

Needs Pillow (pip install pillow).  The result is committed as
MITSchemeIDE.icns, so this only has to be run when the design changes;
make-app.sh does not need Pillow.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 1024


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return m


def vertical_gradient(size, top, bottom):
    img = Image.new("RGB", (size, size), top)
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        c = tuple(round(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(size):
            px[x, y] = c
    return img


def find_font(size):
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def draw_icon(size=SIZE):
    # macOS icons sit inside a ~10% margin; the rounded square is the
    # "tile" the system expects.
    margin = round(size * 0.075)
    tile = size - 2 * margin
    radius = round(tile * 0.225)

    bg = vertical_gradient(tile, (37, 99, 235), (9, 38, 112))     # blue
    tile_img = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
    tile_img.paste(bg, (0, 0), rounded_mask(tile, radius))

    # a soft highlight across the upper half
    glow = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-tile * 0.2, -tile * 0.55, tile * 1.2, tile * 0.55),
                                 fill=(255, 255, 255, 46))
    glow = glow.filter(ImageFilter.GaussianBlur(tile * 0.08))
    tile_img = Image.alpha_composite(tile_img, Image.composite(
        glow, Image.new("RGBA", (tile, tile), (0, 0, 0, 0)), rounded_mask(tile, radius)))

    draw = ImageDraw.Draw(tile_img)
    # parentheses in a muted tone, lambda in white
    paren_font = find_font(round(tile * 0.60))
    lam_font = find_font(round(tile * 0.60))
    cx, cy = tile / 2, tile / 2 + tile * 0.02
    for text, dx, color in (("(", -tile * 0.30, (191, 219, 254, 255)),
                            (")", tile * 0.30, (191, 219, 254, 255))):
        box = draw.textbbox((0, 0), text, font=paren_font, anchor="lt")
        w, h = box[2] - box[0], box[3] - box[1]
        draw.text((cx + dx - w / 2 - box[0], cy - h / 2 - box[1]), text,
                  font=paren_font, fill=color)
    box = draw.textbbox((0, 0), "λ", font=lam_font, anchor="lt")
    w, h = box[2] - box[0], box[3] - box[1]
    # drop shadow, then the glyph
    shadow = Image.new("RGBA", (tile, tile), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((cx - w / 2 - box[0], cy - h / 2 - box[1] + tile * 0.012),
                                "λ", font=lam_font, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(tile * 0.012))
    tile_img = Image.alpha_composite(tile_img, shadow)
    ImageDraw.Draw(tile_img).text((cx - w / 2 - box[0], cy - h / 2 - box[1]), "λ",
                                  font=lam_font, fill=(255, 255, 255, 255))

    # outer shadow under the tile, as macOS does
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sh = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 90), (margin, margin + round(size * 0.02), margin + tile,
                             margin + tile + round(size * 0.02)), rounded_mask(tile, radius))
    sh = sh.filter(ImageFilter.GaussianBlur(size * 0.015))
    icon = Image.alpha_composite(icon, sh)
    icon.alpha_composite(tile_img, (margin, margin))
    return icon


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    icon = draw_icon()
    png = os.path.join(outdir, "MITSchemeIDE.png")
    icns = os.path.join(outdir, "MITSchemeIDE.icns")
    icon.save(png)
    icon.save(icns, format="ICNS")
    print("wrote", png)
    print("wrote", icns)


if __name__ == "__main__":
    main()
