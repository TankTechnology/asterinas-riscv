#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Capture an Xorg root window and hash its actual pixel payload.

QEMU's ``screendump`` has no surface when the guest uses ``egl-headless``.
The DRM desktop therefore observes the pixels at the Xorg boundary with
bounded XGetImage tiles.  A legacy XWD parser remains available for fixture
tests, but the runtime path avoids one multi-megabyte X11 reply that can stall
the server's glamor readback path.
"""

from __future__ import annotations

import argparse
import hashlib
import ctypes
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


class ScreenshotError(ValueError):
    """The XWD payload is not a bounded, complete screenshot."""


@dataclass(frozen=True)
class XwdImage:
    width: int
    height: int
    bits_per_pixel: int
    bytes_per_line: int
    pixels: bytes
    pixel_sha256: str


class _XImagePrefix(ctypes.Structure):
    """The public XImage fields needed to copy a returned ZPixmap."""

    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
    ]


_HEADER_WORDS = 25
_HEADER_BYTES = _HEADER_WORDS * 4
_MAX_BYTES = 64 * 1024 * 1024


def _header(payload: bytes) -> tuple[str, tuple[int, ...]]:
    if len(payload) < _HEADER_BYTES:
        raise ScreenshotError("XWD header is truncated")
    candidates: list[tuple[str, tuple[int, ...]]] = []
    for endian in ("<", ">"):
        values = struct.unpack_from(f"{endian}{_HEADER_WORDS}I", payload)
        header_size, version, _, _, width, height = values[:6]
        if (
            100 <= header_size <= len(payload)
            and version == 7
            and 1 <= width <= 16384
            and 1 <= height <= 16384
        ):
            candidates.append((endian, values))
    if len(candidates) != 1:
        raise ScreenshotError("XWD header byte order is ambiguous or invalid")
    return candidates[0]


def parse_xwd(payload: bytes) -> XwdImage:
    """Parse one bounded XWD image and hash only its complete pixel bytes."""

    if len(payload) > _MAX_BYTES:
        raise ScreenshotError("XWD screenshot exceeds byte cap")
    endian, values = _header(payload)
    header_size, _, _, _, width, height = values[:6]
    bits_per_pixel = values[11]
    bytes_per_line = values[12]
    ncolors = values[18]
    if bits_per_pixel == 0 or bits_per_pixel % 8 or bits_per_pixel > 64:
        raise ScreenshotError("XWD pixel depth is unsupported")
    if bytes_per_line < (width * bits_per_pixel + 7) // 8:
        raise ScreenshotError("XWD stride is truncated")
    if ncolors > 4096:
        raise ScreenshotError("XWD color table is too large")
    image_offset = (header_size + ncolors * 12 + 3) & ~3
    image_size = bytes_per_line * height
    if image_offset > len(payload) or image_size > len(payload) - image_offset:
        raise ScreenshotError("XWD pixel payload is truncated")
    pixels = payload[image_offset : image_offset + image_size]
    del endian  # the pixel bytes are intentionally hashed in their native order
    return XwdImage(
        width=width,
        height=height,
        bits_per_pixel=bits_per_pixel,
        bytes_per_line=bytes_per_line,
        pixels=pixels,
        pixel_sha256=hashlib.sha256(pixels).hexdigest(),
    )


def _payload_summary(payload: bytes) -> str:
    """Return bounded header facts when a server sends an incomplete image."""

    if len(payload) < _HEADER_BYTES:
        return f"payload_bytes={len(payload)} header_bytes={_HEADER_BYTES}"
    facts: list[str] = [f"payload_bytes={len(payload)}"]
    for endian in ("<", ">"):
        values = struct.unpack_from(f"{endian}{_HEADER_WORDS}I", payload)
        header_size, version, _, _, width, height = values[:6]
        if version == 7 and 1 <= width <= 16384 and 1 <= height <= 16384:
            bpp = values[11]
            stride = values[12]
            ncolors = values[18]
            expected = (header_size + ncolors * 12 + 3) & ~3
            expected += stride * height
            facts.extend(
                (
                    f"endian={endian} header_size={header_size}",
                    f"width={width} height={height} bpp={bpp}",
                    f"stride={stride} ncolors={ncolors} expected_bytes={expected}",
                )
            )
            break
    return " ".join(facts)


def capture_tiled(display: str, timeout: float, tile_size: int = 64) -> XwdImage:
    """Read the root pixmap in bounded XGetImage tiles.

    A single root-window ``GetImage`` asks glamor and the X11 stream to carry
    the whole 1280x800 reply at once.  That path can stall even though the
    same virgl FBO reads back promptly.  Tiling keeps every server reply and
    every virgl transfer bounded while hashing exactly the same native pixel
    bytes in screen order.
    """

    if not 16 <= tile_size <= 256:
        raise ScreenshotError("tile size is outside the bounded range")
    try:
        x11 = ctypes.CDLL("libX11.so.6")
        pointer = ctypes.c_void_p
        ulong = ctypes.c_ulong
        x11.XOpenDisplay.restype = pointer
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [pointer]
        x11.XDefaultRootWindow.restype = ulong
        x11.XDefaultRootWindow.argtypes = [pointer]
        x11.XGetGeometry.restype = ctypes.c_int
        x11.XGetGeometry.argtypes = [
            pointer,
            ulong,
            ctypes.POINTER(ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XGetImage.restype = pointer
        x11.XGetImage.argtypes = [
            pointer,
            ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ulong,
            ctypes.c_int,
        ]
        x11.XDestroyImage.restype = ctypes.c_int
        x11.XDestroyImage.argtypes = [pointer]
        display_handle = x11.XOpenDisplay(display.encode("ascii"))
    except OSError as error:
        raise ScreenshotError(f"X11 loader failed: {error}") from error
    if not display_handle:
        raise ScreenshotError("XOpenDisplay failed")

    root = x11.XDefaultRootWindow(display_handle)
    root_return = ulong()
    root_x, root_y = ctypes.c_int(), ctypes.c_int()
    width, height = ctypes.c_uint(), ctypes.c_uint()
    border, depth = ctypes.c_uint(), ctypes.c_uint()
    try:
        if not x11.XGetGeometry(
            display_handle,
            root,
            ctypes.byref(root_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(width),
            ctypes.byref(height),
            ctypes.byref(border),
            ctypes.byref(depth),
        ):
            raise ScreenshotError("XGetGeometry failed")
        if not (1 <= width.value <= 16384 and 1 <= height.value <= 16384):
            raise ScreenshotError("root geometry is outside the bounded range")
        deadline = time.monotonic() + timeout
        pixels = bytearray()
        image_bpp = None
        image_stride = None
        for top in range(0, height.value, tile_size):
            tile_height = min(tile_size, height.value - top)
            for left in range(0, width.value, tile_size):
                if time.monotonic() >= deadline:
                    raise ScreenshotError("tiled XGetImage exceeded its deadline")
                tile_width = min(tile_size, width.value - left)
                image_ptr = x11.XGetImage(
                    display_handle,
                    root,
                    left,
                    top,
                    tile_width,
                    tile_height,
                    (1 << 32) - 1,
                    2,  # ZPixmap
                )
                if not image_ptr:
                    raise ScreenshotError("XGetImage returned NULL")
                try:
                    image = ctypes.cast(image_ptr, ctypes.POINTER(_XImagePrefix)).contents
                    if image.width != tile_width or image.height != tile_height:
                        raise ScreenshotError("XImage geometry differs from the request")
                    if image.bits_per_pixel != 32 or image.bytes_per_line < tile_width * 4:
                        raise ScreenshotError(
                            "XImage does not provide a bounded 32-bit pixel stride"
                        )
                    if image.data is None:
                        raise ScreenshotError("XImage has no pixel data")
                    if image_bpp is None:
                        image_bpp = image.bits_per_pixel
                        image_stride = width.value * 4
                    elif image.bits_per_pixel != image_bpp:
                        raise ScreenshotError("XImage bits-per-pixel changed between tiles")
                    raw = ctypes.string_at(
                        image.data, image.bytes_per_line * tile_height
                    )
                    row_bytes = tile_width * 4
                    for row in range(tile_height):
                        pixels.extend(
                            raw[row * image.bytes_per_line : row * image.bytes_per_line + row_bytes]
                        )
                finally:
                    x11.XDestroyImage(image_ptr)
        if image_bpp is None or image_stride is None:
            raise ScreenshotError("root image contained no tiles")
        return XwdImage(
            width=width.value,
            height=height.value,
            bits_per_pixel=image_bpp,
            bytes_per_line=image_stride,
            pixels=bytes(pixels),
            pixel_sha256=hashlib.sha256(pixels).hexdigest(),
        )
    finally:
        x11.XCloseDisplay(display_handle)


def capture(display: str, timeout: float) -> XwdImage:
    """Capture the root window through xwd with one bounded subprocess."""

    try:
        completed = subprocess.run(
            ["xwd", "-root", "-silent", "-display", display],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScreenshotError(f"xwd capture failed: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:256]
        raise ScreenshotError(f"xwd exited {completed.returncode}: {detail}")
    try:
        return parse_xwd(completed.stdout)
    except ScreenshotError as error:
        raise ScreenshotError(f"{error}; {_payload_summary(completed.stdout)}") from error


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--display", default=":0")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--tile-size", type=int, default=64)
    args = parser.parse_args(arguments)
    try:
        image = capture_tiled(args.display, args.timeout, args.tile_size)
        if args.output is not None:
            args.output.write_text(
                f"{image.width} {image.height} {image.bits_per_pixel} "
                f"{image.bytes_per_line} {image.pixel_sha256}\n",
                encoding="ascii",
            )
        print(
            "DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT "
            f"width={image.width} height={image.height} "
            f"bpp={image.bits_per_pixel} stride={image.bytes_per_line} "
            f"bytes={len(image.pixels)} sha256={image.pixel_sha256}"
        )
        return 0
    except (ScreenshotError, ValueError) as error:
        print(f"DEBIAN_DESKTOP_M4_DRM_X11_SCREENSHOT_FAIL reason={error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
