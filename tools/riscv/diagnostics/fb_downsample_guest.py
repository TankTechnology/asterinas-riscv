"""Downsample the Megrez framebuffer and print it as a base64 PPM.

A full 1920x1080 BGRA capture is 8 MiB, and the console link has no flow
control, so a gzip+base64 dump of it loses bytes and decodes to nothing. This
samples every STEP-th pixel in both directions, which is enough to see whether
the wallpaper, panel and windows are painted, and fits in tens of Ki
uncompressed. Sample points are also printed as text so a run can be checked
without decoding the image at all.

Run as: python3 /tmp/fb_sample.py [step]
"""

import base64
import gzip
import mmap
import sys

WIDTH, HEIGHT = 1920, 1080
STEP = int(sys.argv[1]) if len(sys.argv) > 1 else 8
PROBE_POINTS = ((8, 8), (960, 60), (960, 540), (8, 1040), (960, 1066), (1900, 540))

with open("/dev/fb0", "rb") as fb:
    frame = mmap.mmap(fb.fileno(), WIDTH * HEIGHT * 4, prot=mmap.PROT_READ)
    stride = WIDTH * 4

    def pixel(x, y):
        base = y * stride + x * 4
        blue, green, red = frame[base], frame[base + 1], frame[base + 2]
        return red, green, blue

    for x, y in PROBE_POINTS:
        print("P_%d_%d" % (x, y), "%02x%02x%02x" % pixel(x, y))

    out = bytearray(b"P6\n%d %d\n255\n" % (WIDTH // STEP, HEIGHT // STEP))
    for y in range(0, HEIGHT, STEP):
        row = y * stride
        for x in range(0, WIDTH, STEP):
            base = row + x * 4
            out += bytes((frame[base + 2], frame[base + 1], frame[base]))

body = base64.b64encode(gzip.compress(bytes(out), 6)).decode()
print("B64START")
for start in range(0, len(body), 76):
    print(body[start : start + 76])
print("B64END")
