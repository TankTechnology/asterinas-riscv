#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Draw and read back two pixels through a specific DRM render node.

The GBM device is mandatory: a surfaceless fallback could silently select a
software renderer on a different device. Exit 0 only for the requested
renderer and the expected white/black pixels.
"""

from __future__ import annotations

import argparse
import ctypes as C
import os
import sys


EGL_PLATFORM_GBM_KHR = 0x31D7
EGL_OPENGL_ES_API = 0x30A0
EGL_OPENGL_ES2_BIT = 4
EGL_RENDERABLE_TYPE = 0x3040
EGL_RED_SIZE = 0x3024
EGL_GREEN_SIZE = 0x3023
EGL_BLUE_SIZE = 0x3022
EGL_NONE = 0x3038
EGL_CONTEXT_CLIENT_VERSION = 0x3098

GL_RENDERER = 0x1F01
GL_COLOR_BUFFER_BIT = 0x4000
GL_FRAMEBUFFER = 0x8D40
GL_RENDERBUFFER = 0x8D41
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
GL_RGBA4 = 0x8056
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401
GL_VERTEX_SHADER = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82
GL_TRIANGLE_STRIP = 5
GL_FLOAT = 0x1406


class ProbeError(Exception):
    pass


def bind(lib: C.CDLL, name: str, result: object, *args: object):
    fn = getattr(lib, name)
    fn.restype = result
    fn.argtypes = list(args)
    return fn


def draw(device: str, expected_renderer: str) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
    try:
        egl = C.CDLL("libEGL.so.1")
        gl = C.CDLL("libGLESv2.so.2")
        gbm = C.CDLL("libgbm.so.1")

        gbm_create_device = bind(gbm, "gbm_create_device", C.c_void_p, C.c_int)
        gbm_destroy_device = bind(gbm, "gbm_device_destroy", None, C.c_void_p)
        gbm_device = gbm_create_device(fd)
        if not gbm_device:
            raise ProbeError("gbm-create-device")
        try:
            get_proc = bind(egl, "eglGetProcAddress", C.c_void_p, C.c_char_p)
            address = get_proc(b"eglGetPlatformDisplayEXT")
            if not address:
                raise ProbeError("eglGetPlatformDisplayEXT-unavailable")
            get_display = C.CFUNCTYPE(C.c_void_p, C.c_uint, C.c_void_p, C.c_void_p)(address)
            display = get_display(EGL_PLATFORM_GBM_KHR, gbm_device, None)
            if not display:
                raise ProbeError("egl-display")

            egl_initialize = bind(egl, "eglInitialize", C.c_uint, C.c_void_p, C.c_void_p, C.c_void_p)
            egl_terminate = bind(egl, "eglTerminate", C.c_uint, C.c_void_p)
            if not egl_initialize(display, None, None):
                raise ProbeError("egl-initialize")
            try:
                egl_bind_api = bind(egl, "eglBindAPI", C.c_uint, C.c_uint)
                if not egl_bind_api(EGL_OPENGL_ES_API):
                    raise ProbeError("egl-bind-gles")
                egl_choose_config = bind(
                    egl, "eglChooseConfig", C.c_uint, C.c_void_p, C.c_void_p,
                    C.c_void_p, C.c_int, C.c_void_p,
                )
                attrs = (C.c_int * 9)(
                    EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
                    EGL_RED_SIZE, 4, EGL_GREEN_SIZE, 4, EGL_BLUE_SIZE, 4, EGL_NONE,
                )
                config = C.c_void_p()
                count = C.c_int()
                if not egl_choose_config(display, attrs, C.byref(config), 1, C.byref(count)) or count.value < 1:
                    raise ProbeError("egl-choose-config")
                create_context = bind(
                    egl, "eglCreateContext", C.c_void_p, C.c_void_p, C.c_void_p,
                    C.c_void_p, C.c_void_p,
                )
                destroy_context = bind(egl, "eglDestroyContext", C.c_uint, C.c_void_p, C.c_void_p)
                context_attrs = (C.c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE)
                context = create_context(display, config, None, context_attrs)
                if not context:
                    raise ProbeError("egl-create-context")
                try:
                    make_current = bind(
                        egl, "eglMakeCurrent", C.c_uint, C.c_void_p, C.c_void_p,
                        C.c_void_p, C.c_void_p,
                    )
                    if not make_current(display, None, None, context):
                        raise ProbeError("egl-make-current")
                    renderer_bytes = bind(gl, "glGetString", C.c_char_p, C.c_uint)(GL_RENDERER)
                    renderer = (renderer_bytes or b"").decode(errors="replace")
                    if expected_renderer not in renderer:
                        raise ProbeError(f"renderer-mismatch actual={renderer!r}")

                    gen_renderbuffers = bind(gl, "glGenRenderbuffers", None, C.c_int, C.c_void_p)
                    renderbuffer = C.c_uint()
                    gen_renderbuffers(1, C.byref(renderbuffer))
                    bind(gl, "glBindRenderbuffer", None, C.c_uint, C.c_uint)(GL_RENDERBUFFER, renderbuffer.value)
                    bind(gl, "glRenderbufferStorage", None, C.c_uint, C.c_uint, C.c_int, C.c_int)(GL_RENDERBUFFER, GL_RGBA4, 16, 16)
                    framebuffer = C.c_uint()
                    bind(gl, "glGenFramebuffers", None, C.c_int, C.c_void_p)(1, C.byref(framebuffer))
                    bind(gl, "glBindFramebuffer", None, C.c_uint, C.c_uint)(GL_FRAMEBUFFER, framebuffer.value)
                    bind(gl, "glFramebufferRenderbuffer", None, C.c_uint, C.c_uint, C.c_uint, C.c_uint)(
                        GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, renderbuffer.value,
                    )
                    status = bind(gl, "glCheckFramebufferStatus", C.c_uint, C.c_uint)(GL_FRAMEBUFFER)
                    if status != GL_FRAMEBUFFER_COMPLETE:
                        raise ProbeError(f"framebuffer-status=0x{status:x}")

                    shader_source = bind(gl, "glShaderSource", None, C.c_uint, C.c_int, C.c_void_p, C.c_void_p)
                    compile_shader = bind(gl, "glCompileShader", None, C.c_uint)
                    shader_status = bind(gl, "glGetShaderiv", None, C.c_uint, C.c_uint, C.c_void_p)
                    create_shader = bind(gl, "glCreateShader", C.c_uint, C.c_uint)
                    shaders = []
                    for kind, source in (
                        (GL_VERTEX_SHADER, b"attribute vec2 position; void main() { gl_Position = vec4(position, 0.0, 1.0); }"),
                        (GL_FRAGMENT_SHADER, b"precision mediump float; void main() { gl_FragColor = vec4(1.0); }"),
                    ):
                        shader = create_shader(kind)
                        source_pointer = C.c_char_p(source)
                        shader_source(shader, 1, C.byref(source_pointer), None)
                        compile_shader(shader)
                        compiled = C.c_int()
                        shader_status(shader, GL_COMPILE_STATUS, C.byref(compiled))
                        if not compiled.value:
                            raise ProbeError(f"shader-compile kind={kind}")
                        shaders.append(shader)
                    program = bind(gl, "glCreateProgram", C.c_uint)()
                    attach = bind(gl, "glAttachShader", None, C.c_uint, C.c_uint)
                    for shader in shaders:
                        attach(program, shader)
                    bind(gl, "glLinkProgram", None, C.c_uint)(program)
                    linked = C.c_int()
                    bind(gl, "glGetProgramiv", None, C.c_uint, C.c_uint, C.c_void_p)(program, GL_LINK_STATUS, C.byref(linked))
                    if not linked.value:
                        raise ProbeError("program-link")
                    bind(gl, "glUseProgram", None, C.c_uint)(program)
                    bind(gl, "glViewport", None, C.c_int, C.c_int, C.c_int, C.c_int)(0, 0, 16, 16)
                    bind(gl, "glClearColor", None, C.c_float, C.c_float, C.c_float, C.c_float)(0, 0, 0, 1)
                    bind(gl, "glClear", None, C.c_uint)(GL_COLOR_BUFFER_BIT)
                    location = bind(gl, "glGetAttribLocation", C.c_int, C.c_uint, C.c_char_p)(program, b"position")
                    if location < 0:
                        raise ProbeError("vertex-attribute")
                    vertices = (C.c_float * 8)(-1, -1, 0, -1, -1, 1, 0, 1)
                    bind(gl, "glVertexAttribPointer", None, C.c_uint, C.c_int, C.c_uint, C.c_uint, C.c_int, C.c_void_p)(
                        location, 2, GL_FLOAT, 0, 0, C.cast(vertices, C.c_void_p),
                    )
                    bind(gl, "glEnableVertexAttribArray", None, C.c_uint)(location)
                    bind(gl, "glDrawArrays", None, C.c_uint, C.c_int, C.c_int)(GL_TRIANGLE_STRIP, 0, 4)
                    read = bind(
                        gl, "glReadPixels", None, C.c_int, C.c_int, C.c_int,
                        C.c_int, C.c_uint, C.c_uint, C.c_void_p,
                    )
                    left = (C.c_ubyte * 4)()
                    right = (C.c_ubyte * 4)()
                    read(4, 8, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, left)
                    read(12, 8, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, right)
                    return renderer, tuple(left), tuple(right)
                finally:
                    destroy_context(display, context)
            finally:
                egl_terminate(display)
        finally:
            gbm_destroy_device(gbm_device)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", nargs="?", default="/dev/dri/renderD128")
    parser.add_argument("--expect-renderer", required=True)
    args = parser.parse_args()
    if not args.expect_renderer:
        parser.error("--expect-renderer must not be empty")
    try:
        renderer, left, right = draw(args.device, args.expect_renderer)
        if any(channel < 250 for channel in left[:3]) or any(channel > 5 for channel in right[:3]):
            raise ProbeError(f"pixel-mismatch left={left} right={right}")
        print(f"PIXEL_PROBE ok=yes renderer={renderer!r} left={left} right={right} device={args.device}")
        return 0
    except (OSError, ProbeError) as error:
        print(f"PIXEL_PROBE ok=no reason={error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
