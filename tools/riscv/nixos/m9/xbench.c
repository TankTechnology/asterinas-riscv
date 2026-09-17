// SPDX-License-Identifier: MPL-2.0
//
// DRM-M9 render micro-benchmark: measures X11 drawing throughput for a set of
// primitives (full-screen fill, rectangles, lines, points, 1x1 pixel putimage)
// so the same guest can be compared across the fbdev and modesetting drivers.
//
// Output is written to both stdout and /dev/ttyS0 (the serial console) so the
// boot harness can capture it from the serial log.

#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static FILE *ser;  // serial console mirror

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static void report(const char *label, int iters, double ms) {
    double per_sec = iters / (ms / 1000.0);
    char line[256];
    snprintf(line, sizeof(line), "XBENCH %-20s %6d ops in %9.1f ms  ->  %8.0f ops/sec\n",
             label, iters, ms, per_sec);
    fputs(line, stdout);
    fflush(stdout);
    if (ser) {
        fputs(line, ser);
        fflush(ser);
    }
}

int main(void) {
    ser = fopen("/dev/ttyS0", "w");
    if (ser) fputs("XBENCH start\n", ser), fflush(ser);

    Display *dpy = NULL;
    for (int tries = 0; tries < 60 && !dpy; tries++) {
        dpy = XOpenDisplay(NULL);
        if (!dpy) sleep(1);
    }
    if (!dpy) {
        fputs("XBENCH ERROR: cannot open display\n", stdout);
        if (ser) { fputs("XBENCH ERROR: cannot open display\n", ser); fflush(ser); }
        return 1;
    }
    int screen = DefaultScreen(dpy);
    int W = DisplayWidth(dpy, screen);
    int H = DisplayHeight(dpy, screen);

    unsigned long black = BlackPixel(dpy, screen);
    unsigned long white = WhitePixel(dpy, screen);

    Window w = XCreateSimpleWindow(dpy, RootWindow(dpy, screen), 0, 0, W, H, 0, black, white);
    XMapWindow(dpy, w);
    XSync(dpy, False);

    GC gc = XCreateGC(dpy, w, 0, NULL);
    XSetForeground(dpy, gc, white);

    // ---- 1. full-screen fill (ClearArea) ----
    {
        int n = 20;
        double t0 = now_ms();
        for (int i = 0; i < n; i++) {
            XSetForeground(dpy, gc, (i & 1) ? white : black);
            XFillRectangle(dpy, w, gc, 0, 0, W, H);
        }
        XSync(dpy, False);
        double dt = now_ms() - t0;
        report("fill-rect-fullscreen", n, dt);
    }

    // ---- 2. 500 64x64 rectangles ----
    {
        int n = 40, per = 500;
        double t0 = now_ms();
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < per; j++) {
                int x = (j * 47) % W;
                int y = (j * 29) % H;
                XFillRectangle(dpy, w, gc, x, y, 64, 64);
            }
            XSync(dpy, False);
        }
        double dt = now_ms() - t0;
        report("rect-500", n * per, dt);
    }

    // ---- 3. 500 64x64 rectangles (no per-batch sync) ----
    {
        int n = 40, per = 500;
        double t0 = now_ms();
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < per; j++) {
                int x = (j * 47) % W;
                int y = (j * 29) % H;
                XFillRectangle(dpy, w, gc, x, y, 64, 64);
            }
        }
        XSync(dpy, False);
        double dt = now_ms() - t0;
        report("rect-500-nosync", n * per, dt);
    }

    // ---- 4. 500 lines ----
    {
        int n = 40, per = 500;
        double t0 = now_ms();
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < per; j++) {
                XDrawLine(dpy, w, gc, (j * 53) % W, (j * 19) % H, (j * 71) % W, (j * 37) % H);
            }
        }
        XSync(dpy, False);
        double dt = now_ms() - t0;
        report("line-500", n * per, dt);
    }

    // ---- 5. 1000 points ----
    {
        int n = 20, per = 1000;
        double t0 = now_ms();
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < per; j++) {
                XDrawPoint(dpy, w, gc, (j * 97) % W, (j * 43) % H);
            }
        }
        XSync(dpy, False);
        double dt = now_ms() - t0;
        report("point-1000", n * per, dt);
    }

    // ---- 6. 64x64 PutImage (unbatched image transfer) ----
    //
    // NOTE: XPutImage is pathological on BOTH drivers (no 2D accel): 8000 ops
    // never finished in the 7-minute boot-harness window (M10). Bound the count
    // so xbench still emits "XBENCH done" and lets graphical.target come up.
    {
        XImage *img = XCreateImage(dpy, DefaultVisual(dpy, screen), DefaultDepth(dpy, screen),
                                   ZPixmap, 0, NULL, 64, 64, 32, 0);
        img->data = malloc(img->bytes_per_line * img->height);
        memset(img->data, 0xff, img->bytes_per_line * img->height);
        int n = 4, per = 50;
        double t0 = now_ms();
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < per; j++) {
                int x = (j * 61) % W;
                int y = (j * 31) % H;
                XPutImage(dpy, w, gc, img, 0, 0, x, y, 64, 64);
            }
        }
        XSync(dpy, False);
        double dt = now_ms() - t0;
        report("putimage-64x64", n * per, dt);
        free(img->data);
        img->data = NULL;
        XDestroyImage(img);
    }

    fputs("XBENCH done\n", stdout);
    if (ser) { fputs("XBENCH done\n", ser); fflush(ser); }

    XCloseDisplay(dpy);
    return 0;
}
