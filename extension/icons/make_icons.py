#!/usr/bin/env python
"""Generate the Chrome Web Store icon set for the ffbot extension.

Draws a football-lace glyph (one vertical lace, four cross-stitches) on a
dark rounded square, entirely in code -- no external images. Palette matches
the site (site/privacy.html): dark slate background, #4fd08c accent lace,
off-white stitches.

Outputs icon16.png, icon32.png, icon48.png, icon128.png next to this file.

Uses Pillow when importable (renders once at 1024 px and downscales with
Lanczos). Falls back to a pure-stdlib rasterizer + PNG writer otherwise.

Run:  /Users/sid/ff-draft-bot/.venv/bin/python make_icons.py
"""

import os
import struct
import sys
import zlib

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
SIZES = (16, 32, 48, 128)

# ---- design, in unit coordinates (0..1 across the canvas) -------------------

CORNER_RADIUS = 0.225          # rounded-square corner radius
BG_TOP = (26, 35, 52)          # gradient top    (#1a2334)
BG_BOTTOM = (14, 20, 32)       # gradient bottom (#0e1420)
LACE = (79, 208, 140)          # accent green    (#4fd08c)
STITCH = (230, 235, 243)       # off-white       (#e6ebf3)

# capsules: (x0, y0, x1, y1, width, color) -- endpoints of the center line
SPINE = (0.5, 0.19, 0.5, 0.81, 0.105, LACE)
BAR_XS = (0.30, 0.70)
BAR_YS = (0.33, 0.45, 0.57, 0.69)
BAR_W = 0.085
CAPSULES = [SPINE] + [(BAR_XS[0], y, BAR_XS[1], y, BAR_W, STITCH) for y in BAR_YS]


def bg_at(unit_y):
    """Background gradient color at a unit-space y."""
    t = min(1.0, max(0.0, unit_y))
    return tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))


# ---- Pillow path ------------------------------------------------------------

def render_pillow():
    from PIL import Image, ImageDraw

    master = 1024
    img = Image.new("RGBA", (master, master), (0, 0, 0, 0))

    # gradient background, clipped to a rounded square
    grad = Image.new("RGB", (master, master))
    gd = ImageDraw.Draw(grad)
    for y in range(master):
        gd.line([(0, y), (master, y)], fill=bg_at(y / master))
    mask = Image.new("L", (master, master), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, master - 1, master - 1], radius=CORNER_RADIUS * master, fill=255
    )
    img.paste(grad, (0, 0), mask)

    # lace + stitches as capsules (rounded rectangles with radius = half width)
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1, w, color in CAPSULES:
        half = w * master / 2
        box = [x0 * master - half, y0 * master - half,
               x1 * master + half, y1 * master + half]
        d.rounded_rectangle(box, radius=half, fill=color + (255,))

    for size in SIZES:
        out = img.resize((size, size), Image.LANCZOS)
        out.save(os.path.join(OUT_DIR, "icon%d.png" % size))


# ---- stdlib fallback --------------------------------------------------------

def _seg_dist(px, py, x0, y0, x1, y1):
    """Distance from point to line segment."""
    dx, dy = x1 - x0, y1 - y0
    if dx == dy == 0:
        return ((px - x0) ** 2 + (py - y0) ** 2) ** 0.5
    t = ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)
    t = min(1.0, max(0.0, t))
    cx, cy = x0 + t * dx, y0 + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _in_rounded_square(x, y, r):
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return False
    cx = r if x < r else (1 - r if x > 1 - r else None)
    cy = r if y < r else (1 - r if y > 1 - r else None)
    if cx is None or cy is None:  # edge band, not a corner
        return True
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _sample(x, y):
    """RGBA at a unit-space point (no antialiasing; caller supersamples)."""
    if not _in_rounded_square(x, y, CORNER_RADIUS):
        return (0, 0, 0, 0)
    color = bg_at(y)
    for x0, y0, x1, y1, w, c in CAPSULES:
        if _seg_dist(x, y, x0, y0, x1, y1) <= w / 2:
            color = c
    return color + (255,)


def _png_chunk(tag, data):
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(
        ">I", zlib.crc32(chunk) & 0xFFFFFFFF
    )


def _write_png(path, size, rows):
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)))
        f.write(_png_chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(_png_chunk(b"IEND", b""))


def render_stdlib():
    ss = 4  # supersampling factor per axis
    for size in SIZES:
        rows = []
        for py in range(size):
            row = []
            for px in range(size):
                r = g = b = a = 0
                for sy in range(ss):
                    for sx in range(ss):
                        x = (px + (sx + 0.5) / ss) / size
                        y = (py + (sy + 0.5) / ss) / size
                        sr, sg, sb, sa = _sample(x, y)
                        # accumulate premultiplied
                        r += sr * sa
                        g += sg * sa
                        b += sb * sa
                        a += sa
                n = ss * ss
                if a == 0:
                    row += [0, 0, 0, 0]
                else:
                    row += [round(r / a), round(g / a), round(b / a), round(a / n)]
            rows.append(row)
        _write_png(os.path.join(OUT_DIR, "icon%d.png" % size), size, rows)


def main():
    try:
        import PIL  # noqa: F401
        render_pillow()
        how = "Pillow"
    except ImportError:
        render_stdlib()
        how = "stdlib fallback"

    print("Rendered via %s:" % how)
    ok = True
    for size in SIZES:
        path = os.path.join(OUT_DIR, "icon%d.png" % size)
        exists = os.path.isfile(path)
        ok = ok and exists
        print("  %s  %s" % (path, "%d bytes" % os.path.getsize(path) if exists else "MISSING"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
