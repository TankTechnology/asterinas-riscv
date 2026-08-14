// SPDX-License-Identifier: MPL-2.0
//
// Minimal *reparenting* window manager, for isolating the shadow-framebuffer
// reparenting bug. xwm.c adds only a border (no reparent) and renders client
// content fine; matchbox-window-manager reparents and its client content never
// reaches the framebuffer. This WM does the simplest possible reparent — a
// plain grey frame, no XRender/theme pixmaps — to tell whether the shadow
// driver breaks on reparenting per se, or only on matchbox-wm's theming.

#include <X11/Xlib.h>
#include <stdio.h>
#include <unistd.h>

#define TITLE_H 20
#define BORDER 2

static Display *dpy;
static int screen;
static Window root;
static unsigned long frame_bg, frame_border;

static unsigned long alloc_color(unsigned char r, unsigned char g, unsigned char b) {
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
    dpy = NULL;
    for (int attempt = 0; attempt < 120 && !dpy; attempt++) {
        dpy = XOpenDisplay(NULL);
        if (!dpy)
            sleep(1);
    }
    if (!dpy) {
        fprintf(stderr, "xwm_reparent: cannot open display\n");
        return 1;
    }

    screen = DefaultScreen(dpy);
    root = RootWindow(dpy, screen);
    frame_bg = alloc_color(0x49, 0x61, 0x79);      /* matchbox titlebar grey-blue */
    frame_border = alloc_color(0x00, 0x60, 0xC0);

    /* Root background so we can see the frame against it. */
    {
        unsigned long bg = alloc_color(0x30, 0x30, 0x38);
        GC gc = XCreateGC(dpy, root, 0, NULL);
        XSetForeground(dpy, gc, bg);
        XFillRectangle(dpy, root, gc, 0, 0,
                       DisplayWidth(dpy, screen), DisplayHeight(dpy, screen));
        XFreeGC(dpy, gc);
        XFlush(dpy);
    }

    XSelectInput(dpy, root, SubstructureRedirectMask | SubstructureNotifyMask);
    fprintf(stderr, "xwm_reparent: window manager up\n");

    for (;;) {
        XEvent e;
        XNextEvent(dpy, &e);

        if (e.type != MapRequest)
            continue;

        Window client = e.xmaprequest.window;
        XWindowAttributes ca;
        if (!XGetWindowAttributes(dpy, client, &ca))
            continue;

        /* Plain frame, no theme, no background pixmap. */
        XSetWindowAttributes attr;
        attr.override_redirect = True;
        attr.background_pixel = frame_bg;
        attr.border_pixel = frame_border;
        attr.event_mask = ExposureMask | SubstructureRedirectMask;
        Window frame = XCreateWindow(dpy, root,
                                     ca.x - BORDER, ca.y - TITLE_H - BORDER,
                                     ca.width + 2 * BORDER,
                                     ca.height + TITLE_H + 2 * BORDER,
                                     BORDER,
                                     ca.depth, InputOutput, ca.visual,
                                     CWOverrideRedirect | CWBackPixel |
                                         CWBorderPixel | CWEventMask,
                                     &attr);

        XSetWindowBorderWidth(dpy, client, 0);
        XAddToSaveSet(dpy, client);
        XReparentWindow(dpy, client, frame, BORDER, TITLE_H + BORDER);
        XMapSubwindows(dpy, frame);
        XMapWindow(dpy, frame);
        XFlush(dpy);
    }

    return 0;
}
