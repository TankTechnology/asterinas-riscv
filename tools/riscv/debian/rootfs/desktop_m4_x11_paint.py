#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Paint and verify one deterministic X11 frame through guest fb0."""

from __future__ import annotations

import ctypes as c
import fcntl
import os
import struct
import time


FBIOGET_FSCREENINFO = 0x4602
COLORS = (0xCC6622, 0x22CC66, 0x6622CC)
WINDOW_X = 64
WINDOW_Y = 64
WINDOW_WIDTH = 960
WINDOW_HEIGHT = 640


def _load_x11():
    x = c.CDLL("libX11.so.6")
    pointer = c.c_void_p
    ulong = c.c_ulong
    signatures = (
        ("XOpenDisplay", pointer, [c.c_char_p]),
        ("XDefaultRootWindow", ulong, [pointer]),
        ("XCreateSimpleWindow", ulong, [pointer, ulong, c.c_int, c.c_int, c.c_uint, c.c_uint, c.c_uint, ulong, ulong]),
        ("XChangeWindowAttributes", c.c_int, [pointer, ulong, ulong, pointer]),
        ("XMapRaised", c.c_int, [pointer, ulong]),
        ("XCreateGC", pointer, [pointer, ulong, ulong, pointer]),
        ("XSetForeground", c.c_int, [pointer, pointer, ulong]),
        ("XFillRectangle", c.c_int, [pointer, ulong, pointer, c.c_int, c.c_int, c.c_uint, c.c_uint]),
        ("XSync", c.c_int, [pointer, c.c_int]),
        ("XGetImage", pointer, [pointer, ulong, c.c_int, c.c_int, c.c_uint, c.c_uint, ulong, c.c_int]),
        ("XGetPixel", ulong, [pointer, c.c_int, c.c_int]),
        ("XDestroyImage", c.c_int, [pointer]),
        ("XDestroyWindow", c.c_int, [pointer, ulong]),
        ("XCloseDisplay", c.c_int, [pointer]),
    )
    for name, result, arguments in signatures:
        function = getattr(x, name)
        function.restype = result
        function.argtypes = arguments
    x.XTranslateCoordinates.restype = c.c_int
    x.XTranslateCoordinates.argtypes = [
        pointer, ulong, ulong, c.c_int, c.c_int,
        c.POINTER(c.c_int), c.POINTER(c.c_int), c.POINTER(ulong),
    ]
    return x


class _Attributes(c.Structure):
    _fields_ = [
        ("background_pixmap", c.c_ulong), ("background_pixel", c.c_ulong),
        ("border_pixmap", c.c_ulong), ("border_pixel", c.c_ulong),
        ("bit_gravity", c.c_int), ("win_gravity", c.c_int),
        ("backing_store", c.c_int), ("backing_planes", c.c_ulong),
        ("backing_pixel", c.c_ulong), ("save_under", c.c_int),
        ("event_mask", c.c_long), ("do_not_propagate_mask", c.c_long),
        ("override_redirect", c.c_int), ("colormap", c.c_ulong),
        ("cursor", c.c_ulong),
    ]


def main() -> int:
    x = _load_x11()
    display = x.XOpenDisplay(b":0")
    if not display:
        raise RuntimeError("XOpenDisplay failed")
    window = 0
    descriptor = -1
    try:
        root = x.XDefaultRootWindow(display)
        window = x.XCreateSimpleWindow(display, root, WINDOW_X, WINDOW_Y, WINDOW_WIDTH, WINDOW_HEIGHT, 0, 0, 0)
        attributes = _Attributes(override_redirect=1)
        x.XChangeWindowAttributes(display, window, 1 << 9, c.byref(attributes))
        x.XMapRaised(display, window)
        x.XSync(display, 0)
        screen_x, screen_y, child = c.c_int(), c.c_int(), c.c_ulong()
        if not x.XTranslateCoordinates(
            display, window, root, 0, 0,
            c.byref(screen_x), c.byref(screen_y), c.byref(child),
        ):
            raise RuntimeError("XTranslateCoordinates failed")
        gc = x.XCreateGC(display, window, 0, None)
        for index, color in enumerate(COLORS):
            x.XSetForeground(display, gc, color)
            x.XFillRectangle(display, window, gc, index * 320, 0, 320, WINDOW_HEIGHT)
        x.XSync(display, 0)
        descriptor = os.open("/dev/fb0", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        fixed = bytearray(80)
        fcntl.ioctl(descriptor, FBIOGET_FSCREENINFO, fixed, True)
        stride = struct.unpack_from("=I", fixed, 48)[0]
        for index, color in enumerate(COLORS):
            center_x = index * 320 + 160
            image = x.XGetImage(display, window, center_x, WINDOW_HEIGHT // 2, 1, 1, (1 << 32) - 1, 2)
            if not image:
                raise RuntimeError("XGetImage failed")
            try:
                ximage = x.XGetPixel(image, 0, 0) & 0xFFFFFF
            finally:
                x.XDestroyImage(image)
            # A managed client can be reparented or moved. Read the actual
            # root coordinates instead of the requested window geometry.
            offset = (screen_y.value + WINDOW_HEIGHT // 2) * stride + (screen_x.value + center_x) * 4
            raw = os.pread(descriptor, 4, offset)
            if len(raw) != 4:
                raise RuntimeError("fb0 sample is short")
            fb0 = int.from_bytes(raw, "little") & 0xFFFFFF
            print(
                f"DEBIAN_DESKTOP_M4_X11_SAMPLE band={index} expected={color:06x} "
                f"ximage={ximage:06x} fb0={fb0:06x}", flush=True
            )
            if ximage != color or fb0 != color:
                raise RuntimeError("X11 and fb0 samples differ")
        print("DEBIAN_DESKTOP_M4_X11_READY bands=3", flush=True)
        time.sleep(5)
        return 0
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if window:
            x.XDestroyWindow(display, window)
            x.XSync(display, 0)
        x.XCloseDisplay(display)


if __name__ == "__main__":
    raise SystemExit(main())
