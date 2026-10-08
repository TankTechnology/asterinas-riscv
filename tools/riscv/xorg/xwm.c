// SPDX-License-Identifier: MPL-2.0
//
// Minimal window manager for the Asterinas RISC-V framebuffer chain.
//
// A small WM in the spirit of tinywm: it paints a root-window background, takes
// over map requests via SubstructureRedirect, gives each top-level window a
// colored border, and lets you drag windows with Alt+Button1 and close the
// focused window with Alt+F4. This proves the window-management path
// (map/configure) on this kernel.

#include <X11/Xlib.h>
#include <X11/keysym.h>
#include <X11/cursorfont.h>
#include <stdio.h>
#include <unistd.h>

#define BORDER 3

static Display *dpy;
static int screen;
static Window root;
static unsigned long border_color;

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

int main(void) {
    dpy = NULL;
    for (int attempt = 0; attempt < 120 && !dpy; attempt++) {
        dpy = XOpenDisplay(NULL);
        if (!dpy)
            sleep(1);
    }
    if (!dpy) {
        fprintf(stderr, "xwm: cannot open display\n");
        return 1;
    }

    screen = DefaultScreen(dpy);
    root = RootWindow(dpy, screen);
    border_color = alloc_color(0x00, 0x60, 0xC0);

    /* Root background. */
    {
        unsigned long bg = alloc_color(0x30, 0x30, 0x38);
        GC gc = XCreateGC(dpy, root, 0, NULL);
        XSetForeground(dpy, gc, bg);
        XFillRectangle(dpy, root, gc, 0, 0,
                       DisplayWidth(dpy, screen), DisplayHeight(dpy, screen));
        XFreeGC(dpy, gc);
        XFlush(dpy);
    }

    /* Define a visible cursor. XFixes keeps the cursor hidden until a client
     * calls XDefineCursor, so without this there is no pointer on screen. */
    {
        Cursor cursor = XCreateFontCursor(dpy, XC_left_ptr);
        XDefineCursor(dpy, root, cursor);
        XFreeCursor(dpy, cursor);
        XFlush(dpy);
    }

    XSelectInput(dpy, root, SubstructureRedirectMask | SubstructureNotifyMask);

    /* Alt+Button1 to drag, Alt+F4 to close the focused window. */
    XGrabButton(dpy, Button1, Mod1Mask, root, True,
                ButtonPressMask | ButtonReleaseMask | PointerMotionMask,
                GrabModeAsync, GrabModeAsync, None, None);
    XGrabKey(dpy, XKeysymToKeycode(dpy, XK_F4), Mod1Mask, root, True,
             GrabModeAsync, GrabModeAsync);

    fprintf(stderr, "xwm: window manager up\n");

    for (;;) {
        XEvent e;
        XNextEvent(dpy, &e);

        switch (e.type) {
        case MapRequest: {
            Window w = e.xmaprequest.window;
            XSetWindowBorderWidth(dpy, w, BORDER);
            XSetWindowBorder(dpy, w, border_color);
            XMapWindow(dpy, w);
            XFlush(dpy);
            break;
        }

        case ConfigureRequest: {
            XConfigureRequestEvent *req = &e.xconfigurerequest;
            XWindowChanges changes;
            changes.x = req->x;
            changes.y = req->y;
            changes.width = req->width;
            changes.height = req->height;
            changes.border_width = req->border_width;
            changes.sibling = req->above;
            changes.stack_mode = req->detail;
            XConfigureWindow(dpy, req->window, req->value_mask, &changes);
            break;
        }

        case KeyPress: {
            XKeyEvent *k = &e.xkey;
            if (k->keycode == XKeysymToKeycode(dpy, XK_F4)) {
                Window focus;
                int revert;
                XGetInputFocus(dpy, &focus, &revert);
                if (focus != root && focus != None)
                    XDestroyWindow(dpy, focus);
            }
            break;
        }

        case ButtonPress: {
            XButtonEvent *b = &e.xbutton;
            if (b->button != Button1)
                break;
            Window drag = b->subwindow ? b->subwindow : b->window;
            if (drag == root)
                break;
            int ox = b->x_root;
            int oy = b->y_root;
            for (;;) {
                XEvent ev;
                XMaskEvent(dpy, ButtonReleaseMask | PointerMotionMask, &ev);
                if (ev.type == ButtonRelease)
                    break;
                if (ev.type == MotionNotify) {
                    XMotionEvent *m = &ev.xmotion;
                    int dx = m->x_root - ox;
                    int dy = m->y_root - oy;
                    XWindowAttributes wa;
                    if (XGetWindowAttributes(dpy, drag, &wa))
                        XMoveWindow(dpy, drag, wa.x + dx, wa.y + dy);
                    ox = m->x_root;
                    oy = m->y_root;
                }
            }
            break;
        }
        }
    }

    return 0;
}
