# SPDX-License-Identifier: MPL-2.0
"""Ask the guest's gdk-pixbuf to load an image, without a compiler or headers.

    python3 gdk_pixbuf_probe.py /usr/share/asterinas/desktop-wallpaper.bmp

On a healthy GTK stack this prints ``PIXBUF_OK <path> <w> <h>``. A
``Couldn't recognize the image file format`` failure on a file the kernel can
read (``open``/``mmap`` both verified) points at gdk-pixbuf itself, which is
what the Megrez board shows for every PNG/BMP as of 2026-10-29.
"""

import ctypes
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/root/red.png"

pixbuf = ctypes.CDLL("libgdk_pixbuf-2.0.so.0")
pixbuf.gdk_pixbuf_new_from_file.restype = ctypes.c_void_p
pixbuf.gdk_pixbuf_new_from_file.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
pixbuf.gdk_pixbuf_get_width.restype = ctypes.c_int
pixbuf.gdk_pixbuf_get_width.argtypes = [ctypes.c_void_p]
pixbuf.gdk_pixbuf_get_height.restype = ctypes.c_int
pixbuf.gdk_pixbuf_get_height.argtypes = [ctypes.c_void_p]

gobject = ctypes.CDLL("libgobject-2.0.so.0")


class GError(ctypes.Structure):
    _fields_ = [("domain", ctypes.c_uint32), ("code", ctypes.c_int), ("message", ctypes.c_char_p)]


error = ctypes.c_void_p()
handle = pixbuf.gdk_pixbuf_new_from_file(path.encode(), ctypes.byref(error))
if not handle:
    detail = "?"
    if error.value:
        detail = ctypes.cast(error.value, ctypes.POINTER(GError)).contents.message
    print("PIXBUF_FAIL", path, detail)
else:
    print("PIXBUF_OK", path, pixbuf.gdk_pixbuf_get_width(handle), pixbuf.gdk_pixbuf_get_height(handle))

print("ZZPB")
