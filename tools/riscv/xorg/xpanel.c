// SPDX-License-Identifier: MPL-2.0
//
// Pure-X11 desktop panel for the Asterinas RISC-V framebuffer chain.
//
// This is the "lighter desktop" counterpart to the GTK2 matchbox-panel, which
// is blocked in this environment (gmodule/`.so` plugins + shared GTK). It uses
// only libX11 primitives (no fonts, no glib), so it is guaranteed to run the
// same way xwm.c/xclient.c do:
//
//   * a dark panel bar across the top of the screen,
//   * a 7-segment digital clock (HH:MM:SS) that refreshes every second,
//   * a "start" button (three-line icon) that spawns /usr/bin/xterm on click.
//
// Text is drawn as rectangles (7-segment digits) so we need no X core fonts
// or fontconfig/pango in the initramfs.

#include <X11/Xlib.h>
#include <X11/Xatom.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#define PANEL_H 40
#define MENU_X 8
#define MENU_Y 8
#define MENU_W 24
#define MENU_H (PANEL_H - 16)
/* Second launcher (NetSurf) sits to the right of the start button. */
#define WEB_X (MENU_X + MENU_W + 8)
#define WEB_Y MENU_Y
#define WEB_W 24
#define WEB_H (PANEL_H - 16)

/* 7-segment digit geometry (in pixels). */
#define SEG_T 3
#define DIG_W 16
#define DIG_H 28
#define DIG_GAP 4
#define COLON_W 8

static Display *dpy;
static int screen;
static Window panel;
static GC gc;
static unsigned long bg, fg, accent;
static int clock_x;

static unsigned long alloc_color(unsigned char r, unsigned char g, unsigned char b) {
    Colormap cmap = DefaultColormap(dpy, screen);
    XColor c;
    c.red = r * 257;   /* XColor RGB are 16-bit; expand 8-bit input. */
    c.green = g * 257;
    c.blue = b * 257;
    c.flags = DoRed | DoGreen | DoBlue;
    XAllocColor(dpy, cmap, &c);
    return c.pixel;
}

/* Segment bitmask: a b c d e f g. */
static const unsigned char segs[10] = {
    0x3F, /* 0 */ 0x06, /* 1 */ 0x5B, /* 2 */ 0x4F, /* 3 */ 0x66, /* 4 */
    0x6D, /* 5 */ 0x7D, /* 6 */ 0x07, /* 7 */ 0x7F, /* 8 */ 0x6F, /* 9 */
};

static void rect(int x, int y, int w, int h, unsigned long color) {
    XSetForeground(dpy, gc, color);
    XFillRectangle(dpy, panel, gc, x, y, w, h);
}

/* Draw one 7-segment digit with its top-left at (x, y). */
static void draw_digit(int x, int y, int digit) {
    unsigned char m = segs[digit];
    int w = DIG_W, h = DIG_H, t = SEG_T;
    int mid = h / 2 - t / 2;

    if (m & 0x01) rect(x + t, y, w - 2 * t, t, fg);            /* a top */
    if (m & 0x02) rect(x + w - t, y + t, t, mid - t, fg);       /* b top-right */
    if (m & 0x04) rect(x + w - t, y + h / 2, t, mid - t, fg);   /* c bottom-right */
    if (m & 0x08) rect(x + t, y + h - t, w - 2 * t, t, fg);     /* d bottom */
    if (m & 0x10) rect(x, y + h / 2, t, mid - t, fg);           /* e bottom-left */
    if (m & 0x20) rect(x, y + t, t, mid - t, fg);               /* f top-left */
    if (m & 0x40) rect(x + t, y + mid, w - 2 * t, t, fg);       /* g middle */
}

static void draw_colon(int x, int y) {
    int t = SEG_T;
    rect(x + 2, y + DIG_H / 2 - 6, t, t, fg);
    rect(x + 2, y + DIG_H / 2 + 3, t, t, fg);
}

static void draw_menu_button(void) {
    /* Raised button body + three horizontal "menu" lines. */
    rect(MENU_X, MENU_Y, MENU_W, MENU_H, accent);
    rect(MENU_X + 4, MENU_Y + 6, MENU_W - 8, 2, fg);
    rect(MENU_X + 4, MENU_Y + 11, MENU_W - 8, 2, fg);
    rect(MENU_X + 4, MENU_Y + 16, MENU_W - 8, 2, fg);
}

static void draw_web_button(void) {
    /* Raised button body + a small "web page" glyph (outline + dot). */
    rect(WEB_X, WEB_Y, WEB_W, WEB_H, accent);
    rect(WEB_X + 6, WEB_Y + 5, WEB_W - 12, WEB_H - 10, fg); /* page outline */
    rect(WEB_X + 4, WEB_Y + 4, 3, 3, bg);                   /* fold corner */
    rect(WEB_X + 10, WEB_Y + WEB_H - 9, 3, 3, bg);          /* content dot */
}

static void draw_clock(void) {
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);

    int x = clock_x;
    /* Erase the clock area, then redraw HH:MM:SS. */
    rect(clock_x, 6, DisplayWidth(dpy, screen) - clock_x, DIG_H, bg);
    draw_digit(x, 6, tm.tm_hour / 10);  x += DIG_W + DIG_GAP;
    draw_digit(x, 6, tm.tm_hour % 10);  x += DIG_W + DIG_GAP;
    draw_colon(x, 6);                   x += COLON_W;
    draw_digit(x, 6, tm.tm_min / 10);   x += DIG_W + DIG_GAP;
    draw_digit(x, 6, tm.tm_min % 10);   x += DIG_W + DIG_GAP;
    draw_colon(x, 6);                   x += COLON_W;
    draw_digit(x, 6, tm.tm_sec / 10);   x += DIG_W + DIG_GAP;
    draw_digit(x, 6, tm.tm_sec % 10);
}

static void draw_panel(void) {
    int w = DisplayWidth(dpy, screen);
    rect(0, 0, w, PANEL_H, bg);
    draw_menu_button();
    draw_web_button();
    draw_clock();
}

static void spawn_xterm(void) {
    pid_t pid = fork();
    if (pid == 0) {
        char *argv[] = { "/usr/bin/xterm", NULL };
        execv(argv[0], argv);
        _exit(1);
    }
}

static void spawn_netsurf(void) {
    pid_t pid = fork();
    if (pid == 0) {
        char *argv[] = { "/usr/bin/netsurf-gtk", NULL };
        execv(argv[0], argv);
        _exit(1);
    }
}

int main(void) {
    dpy = NULL;
    for (int attempt = 0; attempt < 120 && !dpy; attempt++) {
        dpy = XOpenDisplay(NULL);
        if (!dpy)
            sleep(1);
    }
    if (!dpy) {
        fprintf(stderr, "xpanel: cannot open display\n");
        return 1;
    }

    signal(SIGCHLD, SIG_IGN); /* reap xterm children */

    screen = DefaultScreen(dpy);
    int w = DisplayWidth(dpy, screen);
    bg = alloc_color(0x20, 0x20, 0x28);
    fg = alloc_color(0xE0, 0xE0, 0xE0);
    accent = alloc_color(0x40, 0x48, 0x58);

    /* Clock occupies 8 glyphs (HH:MM:SS) from the right:
     * 6 digits (DIG_W) + 3 gaps between them (DIG_GAP) + 2 colons (COLON_W),
     * plus the two gaps that precede each colon. */
    clock_x = w - 12 - (6 * DIG_W + 5 * DIG_GAP + 2 * COLON_W);

    panel = XCreateSimpleWindow(dpy, RootWindow(dpy, screen), 0, 0, w, PANEL_H,
                                0, 0, bg);
    /* Tell EWMH window managers (matchbox) that this is a dock/panel, not a
     * normal app window, so it is kept as a full-width top bar instead of
     * being maximized/tiled like the desktop apps. */
    {
        Atom wm_type = XInternAtom(dpy, "_NET_WM_WINDOW_TYPE", False);
        Atom wm_dock = XInternAtom(dpy, "_NET_WM_WINDOW_TYPE_DOCK", False);
        XChangeProperty(dpy, panel, wm_type, XA_ATOM, 32, PropModeReplace,
                        (unsigned char *)&wm_dock, 1);
    }
    XSelectInput(dpy, panel, ExposureMask | ButtonPressMask);
    XMapWindow(dpy, panel);
    gc = XCreateGC(dpy, panel, 0, NULL);

    draw_panel();
    XFlush(dpy);
    fprintf(stderr, "xpanel: panel up\n");

    for (;;) {
        while (XPending(dpy)) {
            XEvent e;
            XNextEvent(dpy, &e);
            if (e.type == Expose) {
                draw_panel();
                XFlush(dpy);
            } else if (e.type == ButtonPress) {
                XButtonEvent *b = &e.xbutton;
                if (b->x >= MENU_X && b->x <= MENU_X + MENU_W &&
                    b->y >= MENU_Y && b->y <= MENU_Y + MENU_H) {
                    spawn_xterm();
                } else if (b->x >= WEB_X && b->x <= WEB_X + WEB_W &&
                           b->y >= WEB_Y && b->y <= WEB_Y + WEB_H) {
                    spawn_netsurf();
                }
            }
        }
        draw_clock();
        XFlush(dpy);
        sleep(1);
    }

    return 0;
}
