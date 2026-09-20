#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Render a known shape, read the pixels back, and say what came out.

The desktop DRM gate decides whether acceleration is in play by reading the
renderer string Mesa reports -- `DEBIAN_DESKTOP_DRM_GL renderer=virgl`. That
string is Mesa saying which Gallium driver it *chose*; it is not the GPU saying
it drew anything. A winsys that initialises and then produces nothing, or a
transport that drops the command stream, reports `renderer=virgl` just as
happily as a working one.

This probe asks the second question. It draws a shape whose result is known in
advance and reads the framebuffer back:

    clear to black
    draw a triangle covering the left half, in white
    read one pixel from the left half and one from the right

Left white and right black means rasterisation ran. Both black means nothing
was drawn; both white means the clear did not take. It is deliberately a
*draw* rather than a clear-to-colour: a constant fill would be satisfied by an
implementation that ignores the command stream entirely, whereas the boundary
between the two pixels exists only if the triangle was actually rasterised.

The display is opened from a GBM device on the DRM node rather than as a
surfaceless display, because that is the path that makes Mesa load
`virtio_gpu_dri.so`. A surfaceless context in the guest would be free to fall
back to swrast, and then the pixels would be right for the wrong reason.

Usage:  egl-pixel-probe.py [device]
Output: one line, `PIXEL_PROBE ok=... left=... right=... renderer=...`,
        plus `PIXEL_PROBE_ERROR <stage> <detail>` on any failure. The caller
        turns that into a gate marker; the exit status is 0 when the pixels
        are as expected and 1 otherwise, so neither the marker nor the status
        has to be parsed to know the answer.
"""

from __future__ import annotations

import ctypes
import os
import sys

# --- EGL ------------------------------------------------------------------
EGL_DEFAULT_DISPLAY = 0
EGL_NO_DISPLAY = 0
EGL_NO_SURFACE = 0
EGL_NO_CONTEXT = 0
EGL_FALSE = 0
EGL_TRUE = 1

EGL_PLATFORM_GBM_KHR = 0x31D7
EGL_PLATFORM_SURFACELESS_MESA = 0x31DD

EGL_SURFACE_TYPE = 0x3033
EGL_PBUFFER_BIT = 0x0001
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_BIT = 0x0008
EGL_RED_SIZE = 0x3024
EGL_GREEN_SIZE = 0x3023
EGL_BLUE_SIZE = 0x3022
EGL_ALPHA_SIZE = 0x3021
EGL_WIDTH = 0x3057
EGL_HEIGHT = 0x3056
EGL_NONE = 0x3038
EGL_OPENGL_API = 0x30A2
EGL_CONTEXT_CLIENT_VERSION = 0x3098
EGL_VENDOR = 0x3053
EGL_VERSION = 0x3054
EGL_EXTENSIONS = 0x3055

# --- GL -------------------------------------------------------------------
GL_COLOR_BUFFER_BIT = 0x00004000
GL_TRIANGLES = 0x0004
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401
GL_RENDERER = 0x1F01
GL_VERSION = 0x1F02
GL_FRAMEBUFFER = 0x8D40
GL_RENDERBUFFER = 0x8D41
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_RGBA8 = 0x8058
GL_FRAMEBUFFER_COMPLETE = 0x8CD5

SIZE = 16
# The triangle covers x in [-1, 0], i.e. the left half of a full-viewport quad.
LEFT_PIXEL = (SIZE // 4, SIZE // 2)
RIGHT_PIXEL = (3 * SIZE // 4, SIZE // 2)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
# Mesa rounds 0.0 and 1.0 exactly, so the two colours the probe asserts on are
# the two that need no tolerance. Nothing here depends on rounding.
TOLERANCE = 2


class ProbeError(Exception):
    """A stage of the probe failed, with the reason the driver gave."""


def _load():
    egl = ctypes.CDLL("libEGL.so.1")
    gl = ctypes.CDLL("libGL.so.1")
    gbm = None
    try:
        gbm = ctypes.CDLL("libgbm.so.1")
    except OSError:
        # Not fatal: the surfaceless fallback below does not need it, and on a
        # host without a DRM node there is nothing for it to open anyway.
        pass
    return egl, gl, gbm


def _extension(egl, name, restype, argtypes):
    """Resolve an EGL extension entry point.

    EGL extensions are not exported by the loader -- `libEGL.so.1` exports the
    core API only, and `eglGetPlatformDisplayEXT` lives behind
    `eglGetProcAddress`. Reaching for `egl.eglGetPlatformDisplayEXT` directly
    raises `AttributeError` rather than reporting a failure, so the lookup has
    to be a step of its own.
    """
    egl.eglGetProcAddress.restype = ctypes.c_void_p
    egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
    address = egl.eglGetProcAddress(name.encode())
    if not address:
        return None
    return ctypes.CFUNCTYPE(restype, *argtypes)(address)


def _open_display(egl, gbm, device):
    """A GBM-backed display, or a surfaceless one if there is no DRM node."""
    get_platform_display = _extension(
        egl, "eglGetPlatformDisplayEXT", ctypes.c_void_p,
        [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)],
    )
    if get_platform_display is None:
        raise ProbeError("display", "eglGetPlatformDisplayEXT is unavailable")

    if gbm is not None and device:
        fd = None
        try:
            fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        except OSError:
            fd = None
        if fd is not None:
            gbm.gbm_create_device.restype = ctypes.c_void_p
            gbm.gbm_create_device.argtypes = [ctypes.c_int]
            gbm_device = gbm.gbm_create_device(fd)
            if gbm_device:
                display = get_platform_display(
                    EGL_PLATFORM_GBM_KHR, ctypes.c_void_p(gbm_device), None
                )
                if display:
                    return display, "gbm"

    display = get_platform_display(
        EGL_PLATFORM_SURFACELESS_MESA, ctypes.c_void_p(EGL_DEFAULT_DISPLAY), None
    )
    if display:
        return display, "surfaceless"

    raise ProbeError("display", "neither a GBM nor a surfaceless EGL display")


def probe(device="/dev/dri/renderD128"):
    """Return a result dict, or raise ProbeError naming the stage that failed."""
    egl, gl, gbm = _load()

    egl.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    egl.eglInitialize.restype = ctypes.c_uint
    egl.eglBindAPI.argtypes = [ctypes.c_uint]
    egl.eglBindAPI.restype = ctypes.c_uint
    egl.eglChooseConfig.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.POINTER(ctypes.c_int),
    ]
    egl.eglChooseConfig.restype = ctypes.c_uint
    egl.eglCreateContext.restype = ctypes.c_void_p
    egl.eglCreateContext.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    ]
    egl.eglMakeCurrent.restype = ctypes.c_uint
    egl.eglMakeCurrent.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    ]
    egl.eglQueryString.restype = ctypes.c_char_p
    egl.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]

    gl.glGetString.restype = ctypes.c_char_p
    gl.glGetString.argtypes = [ctypes.c_uint]
    gl.glClearColor.argtypes = [ctypes.c_float] * 4
    gl.glClear.argtypes = [ctypes.c_uint]
    gl.glViewport.argtypes = [ctypes.c_int] * 4
    gl.glColor3f.argtypes = [ctypes.c_float] * 3
    gl.glBegin.argtypes = [ctypes.c_uint]
    gl.glEnd.argtypes = []
    gl.glVertex2f.argtypes = [ctypes.c_float] * 2
    gl.glReadPixels.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
    ]
    for name, argtypes in (
        ("glGenFramebuffers", [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
        ("glBindFramebuffer", [ctypes.c_uint, ctypes.c_uint]),
        ("glGenRenderbuffers", [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
        ("glBindRenderbuffer", [ctypes.c_uint, ctypes.c_uint]),
        ("glRenderbufferStorage", [ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_int]),
        ("glFramebufferRenderbuffer", [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]),
        ("glCheckFramebufferStatus", [ctypes.c_uint]),
    ):
        getattr(gl, name).argtypes = argtypes
    gl.glCheckFramebufferStatus.restype = ctypes.c_uint

    display, kind = _open_display(egl, gbm, device)

    major, minor = ctypes.c_int(), ctypes.c_int()
    if not egl.eglInitialize(ctypes.c_void_p(display), ctypes.byref(major), ctypes.byref(minor)):
        raise ProbeError("initialize", f"kind={kind}")

    egl.eglBindAPI(EGL_OPENGL_API)

    # No `EGL_SURFACE_TYPE` filter, and no EGL surface at all.
    #
    # A GBM display advertises **zero** pbuffer configs -- measured: 0 pbuffer,
    # 8 window on the reference GPU -- so the pbuffer form of this probe fails
    # at `eglCreatePbufferSurface` with EGL_BAD_CONFIG on every real device. A
    # window surface would need a native window this probe has no reason to
    # own. The rendering therefore goes to an FBO, which is what lets the
    # context be surfaceless.
    config_attrs = (ctypes.c_int * 11)(
        EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_NONE,
    )
    config = ctypes.c_void_p()
    count = ctypes.c_int()
    if not egl.eglChooseConfig(
        ctypes.c_void_p(display), config_attrs, ctypes.byref(config), 1, ctypes.byref(count)
    ) or count.value < 1:
        raise ProbeError("choose-config", f"kind={kind} count={count.value}")

    extensions = egl.eglQueryString(ctypes.c_void_p(display), EGL_EXTENSIONS) or b""
    if b"surfaceless_context" not in extensions:
        raise ProbeError("surfaceless", f"kind={kind} EGL_KHR_surfaceless_context missing")

    context_attrs = (ctypes.c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE)
    context = egl.eglCreateContext(
        ctypes.c_void_p(display), config, ctypes.c_void_p(EGL_NO_CONTEXT), context_attrs
    )
    if not context:
        raise ProbeError("context", f"kind={kind}")

    if not egl.eglMakeCurrent(
        ctypes.c_void_p(display), ctypes.c_void_p(EGL_NO_SURFACE),
        ctypes.c_void_p(EGL_NO_SURFACE), ctypes.c_void_p(context),
    ):
        raise ProbeError("make-current", f"kind={kind}")

    framebuffer = ctypes.c_uint()
    renderbuffer = ctypes.c_uint()
    gl.glGenFramebuffers(1, ctypes.byref(framebuffer))
    gl.glBindFramebuffer(GL_FRAMEBUFFER, framebuffer)
    gl.glGenRenderbuffers(1, ctypes.byref(renderbuffer))
    gl.glBindRenderbuffer(GL_RENDERBUFFER, renderbuffer)
    gl.glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, SIZE, SIZE)
    gl.glFramebufferRenderbuffer(
        GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, renderbuffer
    )
    status = gl.glCheckFramebufferStatus(GL_FRAMEBUFFER)
    if status != GL_FRAMEBUFFER_COMPLETE:
        raise ProbeError("framebuffer", f"kind={kind} status=0x{status:04x}")

    def renderer_name():
        raw = gl.glGetString(GL_RENDERER)
        return raw.decode("utf-8", "replace") if raw else ""

    # Immediate mode is what makes the draw below a one-liner, and it needs a
    # compatibility profile. Mesa gives one to a context that asks for no
    # profile in particular; the version string is reported either way, so a
    # core-profile context shows up in the output instead of being mysterious.
    gl.glViewport(0, 0, SIZE, SIZE)
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(GL_COLOR_BUFFER_BIT)
    gl.glColor3f(1.0, 1.0, 1.0)
    gl.glBegin(GL_TRIANGLES)
    gl.glVertex2f(-1.0, -1.0)
    gl.glVertex2f(0.0, -1.0)
    gl.glVertex2f(0.0, 1.0)
    gl.glEnd()
    # Without this the readback can return before the draw has landed, which is
    # exactly the failure this probe exists to detect -- so it must not be the
    # probe's own source of error.
    gl.glFinish()

    def read(x, y):
        buf = (ctypes.c_ubyte * 4)()
        gl.glReadPixels(x, y, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, ctypes.byref(buf))
        return (buf[0], buf[1], buf[2])

    left = read(*LEFT_PIXEL)
    right = read(*RIGHT_PIXEL)

    version = gl.glGetString(GL_VERSION)
    return {
        "kind": kind,
        "renderer": renderer_name(),
        "gl_version": version.decode("utf-8", "replace") if version else "",
        "left": left,
        "right": right,
        "ok": _close(left, WHITE) and _close(right, BLACK),
    }


def _close(pixel, expected):
    return all(abs(actual - want) <= TOLERANCE for actual, want in zip(pixel, expected))


def main(argv):
    device = argv[1] if len(argv) > 1 else "/dev/dri/renderD128"
    try:
        result = probe(device)
    except ProbeError as error:
        print(f"PIXEL_PROBE_ERROR {error.args[0]} {error.args[1] if len(error.args) > 1 else ''}".rstrip())
        return 2
    except Exception as error:  # noqa: BLE001 - the marker must carry any failure
        print(f"PIXEL_PROBE_ERROR unexpected {type(error).__name__}: {error}")
        return 2

    left = "".join(f"{channel:02x}" for channel in result["left"])
    right = "".join(f"{channel:02x}" for channel in result["right"])
    print(
        f"PIXEL_PROBE ok={'yes' if result['ok'] else 'no'} "
        f"left={left} right={right} expect=ffffff/000000 "
        f"display={result['kind']} renderer={result['renderer']!r} "
        f"gl={result['gl_version']!r}"
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
