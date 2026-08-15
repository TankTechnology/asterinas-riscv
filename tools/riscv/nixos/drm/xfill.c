// SPDX-License-Identifier: MPL-2.0
//
// Minimal X11 client for the DRM-M3 smoke test. Fills the root window with a
// solid blue background and a solid red bar on the left half, so the host
// screendump check has a deterministic, resolution-independent signal that Xorg
// (modesetting) actually presented a frame. Kept to two color allocations so
// the draw is two X round-trips, not a 256-iteration gradient (each XAllocColor
// is a blocking round-trip that is very slow under TCG emulation).

#include <X11/Xlib.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static void log_serial(const char *s) {
    int fd = open("/dev/ttyS0", O_WRONLY);
    if (fd >= 0) {
        write(fd, s, strlen(s));
        write(fd, "\n", 1);
        close(fd);
    }
}

static unsigned long alloc_color(Display *dpy, int screen, unsigned char r,
                                 unsigned char g, unsigned char b) {
    Colormap cmap = DefaultColormap(dpy, screen);
    XColor c;
    c.red = r * 257;
    c.green = g * 257;
    c.blue = b * 257;
    c.flags = DoRed | DoGreen | DoBlue;
    XAllocColor(dpy, cmap, &c);
    return c.pixel;
}

int main(void) {
    Display *dpy = NULL;
    for (int attempt = 0; attempt < 180 && !dpy; attempt++) {
        dpy = XOpenDisplay(NULL);
        if (!dpy)
            sleep(1);
    }
    if (!dpy) {
        log_serial("xfill: cannot open display");
        return 1;
    }
    log_serial("__DRM_XOPEN_OK__");

    int screen = DefaultScreen(dpy);
    Window root = RootWindow(dpy, screen);
    int width = DisplayWidth(dpy, screen);
    int height = DisplayHeight(dpy, screen);
    GC gc = XCreateGC(dpy, root, 0, NULL);

    /* Blue background everywhere. */
    XSetForeground(dpy, gc, alloc_color(dpy, screen, 0x00, 0x00, 0xFF));
    XFillRectangle(dpy, root, gc, 0, 0, width, height);

    /* Solid red bar on the left half. */
    XSetForeground(dpy, gc, alloc_color(dpy, screen, 0xFF, 0x00, 0x00));
    XFillRectangle(dpy, root, gc, 0, 0, width / 2, height);

    XFlush(dpy);
    log_serial("__DRM_XCLIENT_OK__");

    for (;;) {
        XEvent e;
        XNextEvent(dpy, &e);
    }
    return 0;
}
