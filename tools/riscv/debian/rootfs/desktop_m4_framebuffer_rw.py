#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Perform one bounded fbdev write/read/restore transaction."""

from __future__ import annotations

import fcntl
import hashlib
import mmap
import math
import os
from pathlib import Path
import stat
import struct
import sys
import time

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FRAMEBUFFER = Path("/dev/fb0")
RECT_WIDTH = 16
RECT_HEIGHT = 16


class FramebufferError(ValueError):
    pass


def layout(variable: bytes, fixed: bytes) -> tuple[int, int, int, int]:
    if len(variable) != 160 or len(fixed) != 80:
        raise FramebufferError("framebuffer metadata size is invalid")
    width, height, virtual_width, virtual_height, xoffset, yoffset, bpp, gray = struct.unpack_from(
        "=8I", variable
    )
    red = struct.unpack_from("=3I", variable, 32)
    green = struct.unpack_from("=3I", variable, 44)
    blue = struct.unpack_from("=3I", variable, 56)
    alpha = struct.unpack_from("=3I", variable, 68)
    stride = struct.unpack_from("=I", fixed, 48)[0]
    visual = struct.unpack_from("=I", fixed, 36)[0]
    memory_bytes = struct.unpack_from("=I", fixed, 24)[0]
    if (
        not 0 < width <= 8192
        or not 0 < height <= 8192
        or virtual_width != width
        or virtual_height != height
        or xoffset != 0
        or yoffset != 0
        or bpp != 32
        or gray != 0
        or red != (16, 8, 0)
        or green != (8, 8, 0)
        or blue != (0, 8, 0)
        or alpha != (24, 8, 0)
        or visual != 2
        or stride < width * 4
        or stride > width * 4 + 65536
        or stride * height > 64 * 1024 * 1024
        or memory_bytes < stride * height
        or RECT_WIDTH > width
        or RECT_HEIGHT > height
    ):
        raise FramebufferError("unsupported framebuffer layout")
    return width, height, stride, stride * height


def encode_pixel(blue: int, green: int, red: int) -> bytes:
    if any(not isinstance(value, int) or not 0 <= value <= 255 for value in (blue, green, red)):
        raise FramebufferError("pixel component is outside 8-bit range")
    return bytes((blue, green, red, 0xFF))


def run_pattern(device: Path, hold_seconds: float) -> int:
    """Show a bounded three-band frame, then restore every original byte."""

    if not math.isfinite(hold_seconds) or not 0 < hold_seconds <= 30:
        raise FramebufferError("pattern hold must be between 0 and 30 seconds")
    descriptor = os.open(device, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise FramebufferError("framebuffer is not a character device")
        variable = bytearray(160)
        fixed = bytearray(80)
        fcntl.ioctl(descriptor, FBIOGET_VSCREENINFO, variable, True)
        fcntl.ioctl(descriptor, FBIOGET_FSCREENINFO, fixed, True)
        width, height, stride, length = layout(bytes(variable), bytes(fixed))
        rect_bytes = bytearray()
        for row in range(RECT_HEIGHT):
            start = row * stride
            rect_bytes.extend(os.pread(descriptor, RECT_WIDTH * 4, start))
        if len(rect_bytes) != RECT_WIDTH * RECT_HEIGHT * 4:
            raise FramebufferError("framebuffer save is incomplete")
        mapping = mmap.mmap(
            descriptor,
            length,
            flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
        )
        try:
            original = bytes(mapping[:length])
            bands = (
                encode_pixel(0x22, 0x66, 0xCC),
                encode_pixel(0x66, 0xCC, 0x22),
                encode_pixel(0xCC, 0x22, 0x66),
            )
            try:
                band_width = width // len(bands)
                for row in range(height):
                    row_start = row * stride
                    for band, pixel in enumerate(bands):
                        start = row_start + band * band_width * 4
                        end = row_start + (
                            width if band == len(bands) - 1 else (band + 1) * band_width
                        ) * 4
                        mapping[start:end] = pixel * ((end - start) // 4)
                mapping.flush()
                print(
                    f"DEBIAN_DESKTOP_M4_FB_SCANOUT_READY width={width} height={height} "
                    f"stride={stride} bands={len(bands)}",
                    flush=True,
                )
                time.sleep(hold_seconds)
            finally:
                mapping[:] = original
                mapping.flush()
        finally:
            mapping.close()
        restored = bytes(os.pread(descriptor, length, 0))
        if restored != original:
            raise FramebufferError("framebuffer restore mismatch")
        print(
            f"DEBIAN_DESKTOP_M4_FB_SCANOUT_RESTORED width={width} height={height} "
            f"saved_sha256={hashlib.sha256(original).hexdigest()} "
            f"restored_sha256={hashlib.sha256(restored).hexdigest()} status=pass",
            flush=True,
        )
        return 0
    except (OSError, ValueError) as error:
        print(f"desktop-m4-framebuffer-rw: {error}", file=sys.stderr)
        return 2
    finally:
        os.close(descriptor)


def run(device: Path = FRAMEBUFFER) -> int:
    descriptor = os.open(device, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise FramebufferError("framebuffer is not a character device")
        variable = bytearray(160)
        fixed = bytearray(80)
        fcntl.ioctl(descriptor, FBIOGET_VSCREENINFO, variable, True)
        fcntl.ioctl(descriptor, FBIOGET_FSCREENINFO, fixed, True)
        width, height, stride, length = layout(bytes(variable), bytes(fixed))
        rect_bytes = bytearray()
        for row in range(RECT_HEIGHT):
            rect_bytes.extend(os.pread(descriptor, RECT_WIDTH * 4, row * stride))
        if len(rect_bytes) != RECT_WIDTH * RECT_HEIGHT * 4:
            raise FramebufferError("framebuffer save is incomplete")
        mapping = mmap.mmap(
            descriptor,
            length,
            flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
        )
        try:
            pixel = encode_pixel(0x22, 0x66, 0xCC)
            try:
                for row in range(RECT_HEIGHT):
                    start = row * stride
                    mapping[start : start + RECT_WIDTH * 4] = pixel * RECT_WIDTH
                mapping.flush()
                for row in range(RECT_HEIGHT):
                    start = row * stride
                    if mapping[start : start + RECT_WIDTH * 4] != pixel * RECT_WIDTH:
                        raise FramebufferError("framebuffer write readback differs")
            finally:
                for row in range(RECT_HEIGHT):
                    start = row * stride
                    mapping[start : start + RECT_WIDTH * 4] = rect_bytes[
                        row * RECT_WIDTH * 4 : (row + 1) * RECT_WIDTH * 4
                    ]
                mapping.flush()
        finally:
            mapping.close()
        restored = bytearray()
        for row in range(RECT_HEIGHT):
            restored.extend(os.pread(descriptor, RECT_WIDTH * 4, row * stride))
        if bytes(restored) != bytes(rect_bytes):
            raise FramebufferError("framebuffer restore mismatch")
        print(
            f"DEBIAN_DESKTOP_M4_FB_RW width={width} height={height} stride={stride} "
            "bpp=32 format=BgrReserved rect=16x16 status=pass "
            f"saved_sha256={hashlib.sha256(rect_bytes).hexdigest()} "
            f"restored_sha256={hashlib.sha256(restored).hexdigest()}"
        )
        return 0
    except (OSError, ValueError) as error:
        print(f"desktop-m4-framebuffer-rw: {error}", file=sys.stderr)
        return 2
    finally:
        os.close(descriptor)


def _parse_args(arguments: list[str] | None) -> tuple[Path, float | None]:
    values = list(sys.argv[1:] if arguments is None else arguments)
    device = FRAMEBUFFER
    pattern = False
    hold = 0.0
    index = 0
    while index < len(values):
        item = values[index]
        if item == "--device" and index + 1 < len(values):
            device = Path(values[index + 1])
            index += 2
        elif item == "--pattern":
            pattern = True
            index += 1
        elif item == "--hold-seconds" and index + 1 < len(values):
            hold = float(values[index + 1])
            index += 2
        else:
            raise FramebufferError("usage: desktop-m4-framebuffer-rw [--device PATH] [--pattern --hold-seconds N]")
    if pattern:
        return device, hold
    if hold:
        raise FramebufferError("--hold-seconds requires --pattern")
    return device, None


def main(arguments: list[str] | None = None) -> int:
    try:
        device, hold = _parse_args(arguments)
        return run_pattern(device, hold) if hold is not None else run(device)
    except (OSError, ValueError) as error:
        print(f"desktop-m4-framebuffer-rw: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
