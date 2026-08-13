// SPDX-License-Identifier: MPL-2.0
//
// X11 desktop demo client for the Asterinas RISC-V framebuffer chain.
//
// Creates a few top-level windows (three color bars, plus a couple of solid
// "app" windows), maps them, and repaints on Expose. Together with the window
// manager (xwm.c) this makes the X server look like a real desktop and proves
// the full client -> server -> framebuffer render path.

#include <X11/Xlib.h>
#include <stdio.h>
#include <unistd.h>

static Display *dpy;
static int screen;

static unsigned long alloc_color(unsigned char r, unsigned char g, unsigned char b) {
    Colormap cmap = DefaultColormap(dpy, screen);
    XColor c;
    /* XColor's red/green/blue are 16-bit (0..65535); expand 8-bit input. */
    c.red = r * 257;
    c.green = g * 257;
    c.blue = b * 257;
    c.flags = DoRed | DoGreen | DoBlue;
    XAllocColor(dpy, cmap, &c);
    return c.pixel;
}

static void fill(Window w, unsigned long color) {
    XSetWindowBackground(dpy, w, color);
    XClearWindow(dpy, w);
}

static void draw_color_bars(Window w, int width, int height) {
    GC gc = XCreateGC(dpy, w, 0, NULL);
    int bar = height / 3;
    unsigned long colors[3] = {
        alloc_color(0xFF, 0, 0),
        alloc_color(0, 0xFF, 0),
        alloc_color(0, 0, 0xFF),
    };
    for (int i = 0; i < 3; i++) {
        XSetForeground(dpy, gc, colors[i]);
        XFillRectangle(dpy, w, gc, 0, i * bar, width, bar + (i == 2 ? height % 3 : 0));
    }
    XFreeGC(dpy, gc);
}

static Window make_window(int x, int y, int width, int height, int solid,
                          int bars) {
    Window w = XCreateSimpleWindow(dpy, RootWindow(dpy, screen), x, y, width,
                                   height, 1, 0, 0);
    XSelectInput(dpy, w, ExposureMask);

    if (bars)
        draw_color_bars(w, width, height);
    else
        fill(w, solid);

    XMapWindow(dpy, w);
    return w;
}

/* Per-window paint state: index of solid color, or -1 for the bars window. */
enum { NW = 3 };
static Window wins[NW];
static int win_bars[NW];
static int win_w[NW], win_h[NW];
static unsigned long win_color[NW];

static void repaint(int i) {
    if (win_bars[i])
        draw_color_bars(wins[i], win_w[i], win_h[i]);
    else
        fill(wins[i], win_color[i]);
}

int main(void) {
    dpy = NULL;
    for (int attempt = 0; attempt < 120 && !dpy; attempt++) {
        dpy = XOpenDisplay(NULL);
        if (!dpy)
            sleep(1);
    }
    if (!dpy) {
        fprintf(stderr, "xclient: cannot open display\n");
        return 1;
    }

    /* Give the window manager a moment to set SubstructureRedirect so our
     * MapWindow requests are managed (framed) rather than shown bare. */
    sleep(2);

    screen = DefaultScreen(dpy);

    /* Window 0: the RGB color-bar window. */
    win_w[0] = 420;
    win_h[0] = 320;
    win_bars[0] = 1;
    win_color[0] = 0;
    wins[0] = make_window(40, 40, win_w[0], win_h[0], 0, 1);

    /* Window 1: a solid orange "app". */
    win_w[1] = 300;
    win_h[1] = 200;
    win_bars[1] = 0;
    win_color[1] = alloc_color(0xFF, 0x80, 0);
    wins[1] = make_window(520, 360, win_w[1], win_h[1], win_color[1], 0);

    /* Window 2: a solid teal "app". */
    win_w[2] = 260;
    win_h[2] = 180;
    win_bars[2] = 0;
    win_color[2] = alloc_color(0, 0x90, 0x90);
    wins[2] = make_window(180, 620, win_w[2], win_h[2], win_color[2], 0);

    XFlush(dpy);
    fprintf(stderr, "xclient: %d windows mapped\n", NW);

    for (;;) {
        XEvent e;
        XNextEvent(dpy, &e);
        if (e.type == Expose) {
            for (int i = 0; i < NW; i++) {
                if (e.xexpose.window == wins[i]) {
                    repaint(i);
                    break;
                }
            }
        }
    }

    return 0;
}
