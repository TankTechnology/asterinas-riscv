#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Pixel-validate a QEMU bochs-display framebuffer screenshot (P6 PPM).

Scores the screenshot of the systemd desktop into one of three screen states and
prints a one-line summary, exiting 0 only for a *rendered* page. This complements
net_validate.sh, which scores the NetSurf fetch outcome from the serial log: a
`code7`/`code56` fetch failure leaves Xorg's uniform gray root weave (a handful of
distinct colors), while a successful render draws antialiased text + a white page
background (hundreds of distinct gray levels). The calibration screenshots are:

    rendered (NetSurf home page)   distinct=213  white=41%  black=24%  content=35%
    code7    (fetch failed)        distinct=25   white=0%   black=0.5% content=99%

Usage:
    python3 pixel_validate.py shot.ppm [--distinct N] [--black-pct N]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Luminance buckets (0..255).
BLACK = 24   # below this -> black (text / panel dark regions)
WHITE = 230  # above this -> white (page background)

# A rendered page produces far more distinct quantized colors than Xorg's
# ~2-tone gray root weave (25 in the code7 calibration); antialiased text alone
# pushes distinct well past this. Chosen between the two observed extremes.
DISTINCT_RENDERED = 40


def validate(path: Path, distinct_thresh: int, black_pct_thresh: float) -> dict:
    with open(path, "rb") as f:
        magic = f.readline().strip()
        if magic not in (b"P6", b"P5"):
            raise SystemExit(f"{path}: not a P6/P5 PPM (magic {magic!r})")
        line = f.readline()
        while line.startswith(b"#"):
            line = f.readline()
        w, h = map(int, line.split())
        maxval = int(f.readline().strip())
        data = f.read()

    total = w * h
    # P5 is single-channel (grayscale); P6 is RGB. QEMU screendump emits P6.
    channels = 3 if magic == b"P6" else 1
    if len(data) < total * channels:
        raise SystemExit(f"{path}: short pixel data ({len(data)} < {total * channels})")

    black = white = content = 0
    distinct: set[tuple[int, int, int]] = set()
    for i in range(total):
        if channels == 3:
            r = data[i * 3]
            g = data[i * 3 + 1]
            b = data[i * 3 + 2]
        else:
            r = g = b = data[i]
        lum = (299 * r + 587 * g + 114 * b) // 1000
        if lum < BLACK:
            black += 1
        elif lum > WHITE:
            white += 1
        else:
            content += 1
        distinct.add((r >> 4, g >> 4, b >> 4))

    n = max(total, 1)
    black_pct = black * 100.0 / n
    white_pct = white * 100.0 / n
    content_pct = content * 100.0 / n

    if len(distinct) >= distinct_thresh:
        state = "rendered"
    elif black_pct > black_pct_thresh:
        state = "black"
    else:
        state = "empty-root"

    return {
        "w": w, "h": h, "maxval": maxval,
        "black_pct": black_pct, "white_pct": white_pct,
        "content_pct": content_pct, "distinct": len(distinct),
        "state": state,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ppm", type=Path)
    ap.add_argument("--distinct", type=int, default=DISTINCT_RENDERED)
    ap.add_argument("--black-pct", type=float, default=95.0)
    args = ap.parse_args()

    r = validate(args.ppm, args.distinct, args.black_pct)
    print(
        f"{args.ppm.name}: {r['state']} "
        f"({r['w']}x{r['h']}, white={r['white_pct']:.1f}%, "
        f"black={r['black_pct']:.1f}%, content={r['content_pct']:.1f}%, "
        f"distinct={r['distinct']})"
    )
    return 0 if r["state"] == "rendered" else 1


if __name__ == "__main__":
    sys.exit(main())
