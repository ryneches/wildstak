#!/usr/bin/env python3
"""
masthead.py - draw the WildStak README masthead: a 1-bit System 6 Finder
desktop with the Get Info window open on the WildStak stack. Behind it, in
the Stacks folder, sits ResEdit: the tool that showed a file's type and
creator (STAK/WILD for a stack).

Every type and creator code tells the Finder which application owns a file;
a HyperCard stack was type STAK, creator WILD (left over from HyperCard's
codename, WildCard). Hence the name.

Drawn in integer device pixels at 400x240 with PIL ImageDraw (pixel fonts at
their native sizes, no antialiasing), then scaled 2x nearest-neighbour to
800x480: the reTerminal E1001's own resolution, so the same PNG works as a
card on the device.

Fonts (not bundled; Chicago and Geneva are Apple designs, so only the
rendered PNG goes in the repo):
  Chicago 12: "Chicago v0.5.5.ttf" from github.com/nikdog/chicago-font, at
      12 px (renders pixel-identical to the Chicago 12 bitmap in 1-bit).
  Geneva 9: "Geneva 9.1" (geneva-9-1.otf), at 16 px (cap height 7, the
      bitmap Geneva 9 at 1:1).

Usage:
    masthead.py render --chicago "Chicago v0.5.5.ttf" --geneva geneva-9-1.otf -o assets/masthead.png
    masthead.py probe FONT.ttf    # cap height and width of "H" at each size
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

W, H = 400, 240
SCALE = 2
BLACK, WHITE = 0, 255

TITLE = "Info"
NAME = "WildStak"
ROWS = [
    ("Kind:", "HyperCard stack"),
    ("Size:", "12K on disk (11,264 bytes used)"),
    ("Where:", "Home: Stacks:"),
    None,
    ("Created:", "Fri, Nov 13, 2026, 3:14 PM"),
    ("Modified:", "Fri, Nov 13, 2026, 3:14 PM"),
]
COMMENTS = ["E-ink cards for your home, drawn", "from Home Assistant."]
MENUS = ["File", "Edit", "View", "Special"]
FOLDER = "Stacks"


# ---------------------------------------------------------------------------
# Icons: 32x32 bitmap + mask, drawn with rectangles so they stay on the grid.
# ---------------------------------------------------------------------------


def _icon_canvas() -> tuple[Image.Image, Image.Image, ImageDraw.ImageDraw, ImageDraw.ImageDraw]:
    bits = Image.new("1", (32, 32), WHITE)
    mask = Image.new("1", (32, 32), 0)
    return bits, mask, ImageDraw.Draw(bits), ImageDraw.Draw(mask)


def stack_icon() -> tuple[Image.Image, Image.Image]:
    """Three offset index cards; the front one carries a little bar chart."""
    bits, mask, d, m = _icon_canvas()
    for x0, y0 in ((8, 3), (5, 7), (2, 11)):
        box = (x0, y0, x0 + 22, y0 + 16)
        d.rectangle(box, fill=WHITE, outline=BLACK)
        m.rectangle(box, fill=255)
    d.line((4, 14, 22, 14), fill=BLACK)              # index-card header rule
    for i, h in enumerate((3, 6, 4, 9, 5, 7)):        # energy bars
        x = 5 + 3 * i
        d.rectangle((x, 25 - h, x + 1, 25), fill=BLACK)
    return bits, mask


def disk_icon() -> tuple[Image.Image, Image.Image]:
    """System 6 style external hard disk: a flat box with an activity light."""
    bits, mask, d, m = _icon_canvas()
    d.rectangle((1, 10, 30, 22), fill=WHITE, outline=BLACK)
    d.line((2, 23, 30, 23), fill=BLACK)               # base shadow
    d.rectangle((4, 18, 6, 19), fill=BLACK)           # light
    # The mask follows the shadow's inset start, or the pattern shows a
    # stray white pixel at the bottom-left corner.
    m.rectangle((1, 10, 30, 22), fill=255)
    m.line((2, 23, 30, 23), fill=255)
    return bits, mask


def trash_icon() -> tuple[Image.Image, Image.Image]:
    bits, mask, d, m = _icon_canvas()
    d.rectangle((13, 2, 18, 4), fill=WHITE, outline=BLACK)   # handle
    d.rectangle((6, 5, 25, 8), fill=WHITE, outline=BLACK)    # lid
    d.rectangle((8, 8, 23, 30), fill=WHITE, outline=BLACK)   # can
    for x in (12, 16, 20):
        d.line((x, 11, x, 27), fill=BLACK)
    for box in ((13, 2, 18, 4), (6, 5, 25, 8), (8, 8, 23, 30)):
        m.rectangle(box, fill=255)
    return bits, mask


# ResEdit's icon, a jester on a spring bursting out of a compact Mac (traced
# from ResEdit 2.1.1's splash screen). It sits in a Finder window behind the
# Get Info window, so only the part right of RESEDIT_CUT is ever visible, and
# only that part is stored here: the first RESEDIT_CUT columns of the
# original 23 are left out.
RESEDIT_CUT = 6
RESEDIT_W = 23
RESEDIT = """
##...............
###..............
####.##..........
#...#..#.........
..##...#.........
......#..........
#.##...#.........
#.##.#.#.........
.....#.##........
...##..#.#.......
.#....#..##......
#.####..#.#......
.....#.#..##.....
......#..#..#....
...#############.
..#.............#
..#..#########..#
..#.#.........#.#
..#.#.#.#.....#.#
..#.#.........#.#
..#.#.#.......#.#
..#.#.........#.#
..#.#.#.#.#...#.#
..#.#.........#.#
..#..#########..#
..#.............#
..#.............#
..#.......#####.#
..#.............#
..#.............#
...#############.
...#############.
"""


def house_glyph(d: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """11x11 house for the menu bar, where the Apple menu would be."""
    for i in range(6):                                # roof
        d.line((x + 5 - i, y + i, x + 5 + i, y + i), fill=BLACK)
    d.rectangle((x + 1, y + 5, x + 9, y + 10), fill=BLACK)
    d.rectangle((x + 4, y + 7, x + 6, y + 10), fill=WHITE)   # door


def paste_icon(img: Image.Image, icon: tuple[Image.Image, Image.Image],
               x: int, y: int, selected: bool = False) -> None:
    bits, mask = icon
    if selected:
        bits = ImageChops.invert(bits)
    img.paste(bits, (x, y), mask)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def text_w(font: ImageFont.FreeTypeFont, s: str) -> int:
    return round(font.getlength(s))


def desktop_label(d: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont,
                  cx: int, top: int, s: str, selected: bool) -> None:
    """Icon name under a desktop icon: white box, or inverted when selected."""
    w = text_w(font, s)
    asc, desc = font.getmetrics()
    box = (cx - w // 2 - 2, top, cx + (w + 1) // 2 + 1, top + asc + desc)
    d.rectangle(box, fill=BLACK if selected else WHITE)
    d.text((cx - w // 2, top + asc), s, font=font, anchor="ls",
           fill=WHITE if selected else BLACK)


def render(chicago: ImageFont.FreeTypeFont, geneva: ImageFont.FreeTypeFont) -> Image.Image:
    img = Image.new("1", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"

    # Desktop: the stock 50% gray pattern.
    px = img.load()
    assert px is not None
    for y in range(H):
        for x in range(W):
            px[x, y] = BLACK if (x + y) % 2 == 0 else WHITE

    # Menu bar.
    mb_h = 20
    d.rectangle((0, 0, W - 1, mb_h - 2), fill=WHITE)
    d.line((0, mb_h - 1, W - 1, mb_h - 1), fill=BLACK)
    baseline = 14
    house_glyph(d, 10, 4)
    x = 34
    for menu in MENUS:
        d.text((x, baseline), menu, font=chicago, anchor="ls", fill=BLACK)
        x += text_w(chicago, menu) + 14

    # Rounded screen corners, as on the compact Macs.
    rad = 5
    for cx, cy in ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)):
        for yy in range(rad):
            for xx in range(rad):
                dx, dy = rad - xx - 0.5, rad - yy - 0.5
                if dx * dx + dy * dy > rad * rad:
                    px[abs(cx - xx), abs(cy - yy)] = BLACK

    # Get Info window: size it to its content.
    c_asc, c_desc = chicago.getmetrics()
    g_asc, g_desc = geneva.getmetrics()
    pad = 10
    row_h = c_asc + c_desc
    label_w = max(text_w(chicago, r[0]) for r in ROWS if r) + 6
    value_w = max(text_w(geneva, r[1]) for r in ROWS if r)
    win_w = max(pad + label_w + value_w + pad, 230)
    title_h = 19
    icon_row_h = 40
    gap_h = 4
    box_h = len(COMMENTS) * (g_asc + g_desc) + 6
    body_h = (icon_row_h + 4
              + sum(row_h if r else gap_h for r in ROWS)
              + gap_h + row_h + box_h + pad)
    x0, y0 = 14, mb_h + 6
    x1, y1 = x0 + win_w, y0 + title_h + body_h

    # Behind it, an inactive Finder window on the Stacks folder (Where:
    # Home: Stacks:), with ResEdit's icon half under the Get Info window.
    # Inactive System 6 windows: plain title bar, blank scroll bars.
    fx0, fy0 = x1 - 24, y0 + 30
    fx1 = W - 68
    sb = 15                                          # scroll bar width
    hy = fy0 + title_h + g_asc + 2                   # header baseline
    hl = hy + g_desc + 2                             # header rules
    ry = hl + 10                                     # icon top
    fy1 = ry + 33 + g_asc + g_desc + 6 + sb
    d.rectangle((fx0 + 1, fy0 + 1, fx1 + 1, fy1 + 1), fill=BLACK)
    d.rectangle((fx0, fy0, fx1, fy1), fill=WHITE, outline=BLACK)
    ftb = fy0 + title_h - 1
    d.line((fx0, ftb, fx1, ftb), fill=BLACK)
    ftw = text_w(chicago, FOLDER)
    ftx = (fx0 + fx1) // 2 - ftw // 2
    if ftx - 6 <= x1 + 1:
        raise SystemExit(f"Finder window title hidden: starts at {ftx}, Get Info ends at {x1 + 1}")
    d.text((ftx, fy0 + 14), FOLDER, font=chicago, anchor="ls", fill=BLACK)
    avail = "802K available"
    ax = fx1 - 5 - text_w(geneva, avail)
    if ax <= x1 + 1:
        raise SystemExit(f"Finder header hidden: starts at {ax}, Get Info ends at {x1 + 1}")
    d.text((ax, hy), avail, font=geneva, anchor="ls", fill=BLACK)
    d.line((fx0, hl, fx1, hl), fill=BLACK)
    d.line((fx0, hl + 2, fx1, hl + 2), fill=BLACK)
    d.rectangle((fx1 - sb, hl + 2, fx1, fy1), outline=BLACK)
    d.rectangle((fx0, fy1 - sb, fx1, fy1), outline=BLACK)
    d.rectangle((fx1 - sb, fy1 - sb, fx1, fy1), outline=BLACK)
    # The stored sprite starts at RESEDIT_CUT; put that column just right of
    # the Get Info shadow, so everything left out would be under the window.
    sprite = RESEDIT.strip().splitlines()
    rx = x1 + 2                                      # first visible column
    for yy, line in enumerate(sprite):
        for xx, c in enumerate(line):
            if c == "#":
                px[rx + xx, ry + yy] = BLACK
    icon_cx = rx - RESEDIT_CUT - (32 - RESEDIT_W) // 2 + 16
    lw = text_w(geneva, "ResEdit")
    d.text((icon_cx - lw // 2, ry + 33 + g_asc), "ResEdit", font=geneva, anchor="ls", fill=BLACK)

    d.rectangle((x0 + 1, y0 + 1, x1 + 1, y1 + 1), fill=BLACK)   # drop shadow
    d.rectangle((x0, y0, x1, y1), fill=WHITE, outline=BLACK)

    # Title bar: pinstripes, close box, title in a white gap.
    tb = y0 + title_h - 1
    d.line((x0, tb, x1, tb), fill=BLACK)
    for yy in range(y0 + 4, y0 + 15, 2):
        d.line((x0 + 2, yy, x1 - 2, yy), fill=BLACK)
    cb = x0 + 8
    d.rectangle((cb - 1, y0 + 3, cb + 11, y0 + 15), fill=WHITE)
    d.rectangle((cb, y0 + 4, cb + 10, y0 + 14), fill=WHITE, outline=BLACK)
    tw = text_w(chicago, TITLE)
    tx = (x0 + x1) // 2 - tw // 2
    d.rectangle((tx - 6, y0 + 1, tx + tw + 5, tb - 1), fill=WHITE)
    d.text((tx, y0 + 14), TITLE, font=chicago, anchor="ls", fill=BLACK)

    # Icon, name, Locked checkbox.
    by = tb + 1
    stack = stack_icon()
    paste_icon(img, stack, x0 + pad, by + 4)
    d.text((x0 + pad + 32 + 10, by + 4 + 20), NAME, font=chicago, anchor="ls", fill=BLACK)
    lock_w = text_w(chicago, "Locked")
    lx = x1 - pad - lock_w - 16
    d.rectangle((lx, by + 10, lx + 10, by + 20), outline=BLACK)
    d.text((lx + 16, by + 19), "Locked", font=chicago, anchor="ls", fill=BLACK)
    y = by + icon_row_h
    d.line((x0 + 1, y, x1 - 1, y), fill=BLACK)
    y += 4

    # Info rows: Chicago labels, Geneva values, on a shared baseline.
    for row in ROWS:
        if row is None:
            y += gap_h
            continue
        label, value = row
        base = y + c_asc
        d.text((x0 + pad, base), label, font=chicago, anchor="ls", fill=BLACK)
        d.text((x0 + pad + label_w, base), value, font=geneva, anchor="ls", fill=BLACK)
        y += row_h
    y += gap_h

    # Comments.
    d.text((x0 + pad, y + c_asc), "Comments:", font=chicago, anchor="ls", fill=BLACK)
    y += row_h
    d.rectangle((x0 + pad, y, x1 - pad, y + box_h), outline=BLACK)
    ty = y + 3 + g_asc
    for line in COMMENTS:
        d.text((x0 + pad + 4, ty), line, font=geneva, anchor="ls", fill=BLACK)
        ty += g_asc + g_desc

    # Desktop icons down the right edge, Finder style: disk at the top,
    # Trash at the bottom, and the stack (selected: it's the one being
    # inspected) in between.
    col = W - 40
    paste_icon(img, disk_icon(), col - 16, mb_h + 8)
    desktop_label(d, geneva, col, mb_h + 8 + 33, "Home", selected=False)
    sy = (mb_h + H) // 2 - 24
    paste_icon(img, stack, col - 16, sy, selected=True)
    desktop_label(d, geneva, col, sy + 33, NAME, selected=True)
    ty = H - 32 - 18
    paste_icon(img, trash_icon(), col - 16, ty)
    desktop_label(d, geneva, col, ty + 33, "Trash", selected=False)

    if y1 + 1 >= H:
        raise SystemExit(f"window too tall: bottom at {y1 + 1}, canvas {H}")
    if fx1 + 1 >= col - 24:
        raise SystemExit(f"Finder window too wide: right edge at {fx1 + 1}, icons from {col - 24}")
    return img


def probe(path: Path) -> None:
    for size in range(6, 33):
        f = ImageFont.truetype(str(path), size)
        bb = f.getbbox("H")
        asc, desc = f.getmetrics()
        print(f"size {size:2d}: H {bb[2] - bb[0]}x{bb[3] - bb[1]}  ascent {asc} descent {desc}")


def main() -> None:
    p = argparse.ArgumentParser(description="Draw the WildStak README masthead.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--chicago", type=Path, required=True)
    r.add_argument("--chicago-size", type=int, default=12)
    r.add_argument("--geneva", type=Path, required=True)
    r.add_argument("--geneva-size", type=int, default=16)
    r.add_argument("-o", "--output", type=Path, default=Path("masthead.png"))
    r.add_argument("--native", action="store_true", help="skip the 2x scale-up")
    pr = sub.add_parser("probe")
    pr.add_argument("font", type=Path)
    args = p.parse_args()

    if args.cmd == "probe":
        probe(args.font)
        return
    chicago = ImageFont.truetype(str(args.chicago), args.chicago_size)
    geneva = ImageFont.truetype(str(args.geneva), args.geneva_size)
    img = render(chicago, geneva)
    if not args.native:
        img = img.resize((W * SCALE, H * SCALE), Image.Resampling.NEAREST)
    img.save(args.output)
    print(f"Wrote {args.output} ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
