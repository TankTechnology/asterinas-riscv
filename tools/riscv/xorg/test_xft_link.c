// SPDX-License-Identifier: MPL-2.0
/*
 * Static-link sanity test for the Phase-1 libX* extension libraries.
 * Exercises libXft (-> libXrender/freetype/fontconfig), libXcursor,
 * libXrandr, libXfixes, libXdamage, libXcomposite, libXinerama, libXi
 * so a clean build here proves the full X11 extension closure links.
 *
 * Build (from repo root):
 *   export PKG_CONFIG_LIBDIR="$PWD/target/riscv-cross/usr/lib/pkgconfig:$PWD/target/riscv-cross/usr/share/pkgconfig"
 *   riscv64-linux-gnu-gcc -static -o test_xft_link tools/riscv/xorg/test_xft_link.c \
 *       $(pkg-config --static --cflags --libs xft xcursor xrandr xfixes xdamage xcomposite xinerama xi)
 *   file test_xft_link   # expect: riscv64 ... statically linked
 */
#include <X11/Xlib.h>
#include <X11/extensions/Xrender.h>
#include <X11/extensions/Xrandr.h>
#include <X11/extensions/Xfixes.h>
#include <X11/extensions/Xdamage.h>
#include <X11/extensions/Xcomposite.h>
#include <X11/extensions/Xinerama.h>
#include <X11/extensions/XInput2.h>
#include <X11/Xft/Xft.h>
#include <X11/Xcursor/Xcursor.h>

int main(void) {
    Display *dpy = XOpenDisplay(NULL);
    if (!dpy)
        return 1;

    /* libXft (pulls freetype + fontconfig + Xrender) */
    XftInit(0);
    XftDraw *draw = XftDrawCreate(dpy, DefaultRootWindow(dpy),
                                  DefaultVisual(dpy, DefaultScreen(dpy)),
                                  DefaultColormap(dpy, DefaultScreen(dpy)));
    XftFont *font = XftFontOpenName(dpy, DefaultScreen(dpy), "sans-12");
    if (draw) XftDrawDestroy(draw);
    if (font) XftFontClose(dpy, font);

    /* libXrender */
    XRenderFindVisualFormat(dpy, DefaultVisual(dpy, DefaultScreen(dpy)));

    /* libXcursor */
    Cursor c = XcursorLibraryLoadCursor(dpy, "left_ptr");
    if (c) XFreeCursor(dpy, c);

    /* libXrandr */
    XRRGetScreenResources(dpy, DefaultRootWindow(dpy));

    /* libXfixes */
    XFixesQueryVersion(dpy, NULL, NULL);

    /* libXinerama */
    XineramaQueryScreens(dpy, NULL);

    /* libXi */
    XIQueryVersion(dpy, NULL, NULL);

    XCloseDisplay(dpy);
    return 0;
}
