#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
W=/var/tmp/im6; rm -rf "$W"; mkdir -p "$W"; mkdir -p /tmp/.X11-unix
if ! command -v gcc >/dev/null 2>&1 || [ ! -f /usr/include/X11/Xlib.h ]; then
    timeout 240 apt-get update >"$W/apt.log" 2>&1
    timeout 300 apt-get install -y --no-install-recommends gcc libc6-dev libx11-dev >>"$W/apt.log" 2>&1
fi
cat >"$W/imtype.c" <<'CEOF'
#define _GNU_SOURCE
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <locale.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
static char out[256]; static size_t outlen;
static void feed(Display *d, Window w, XIC ic, KeySym ks) {
    XKeyEvent ke; memset(&ke, 0, sizeof ke);
    ke.type = KeyPress; ke.display = d; ke.window = w; ke.root = DefaultRootWindow(d);
    ke.subwindow = None; ke.time = CurrentTime; ke.x = 1; ke.y = 1; ke.x_root = 1; ke.y_root = 1;
    ke.same_screen = True; ke.keycode = XKeysymToKeycode(d, ks); ke.state = 0;
    XEvent ev; memset(&ev, 0, sizeof ev); ev.xkey = ke;
    Bool filtered = XFilterEvent(&ev, w);
    char buf[64]; KeySym rk; Status st;
    int n = Xutf8LookupString(ic, &ev.xkey, buf, sizeof buf, &rk, &st);
    printf("FT keysym 0x%lx filtered=%d status=%d n=%d\n", ks, (int)filtered, (int)st, n);
    if (n > 0 && outlen + (size_t)n < sizeof out) { memcpy(out + outlen, buf, (size_t)n); outlen += (size_t)n; }
}
int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    setlocale(LC_ALL, "");
    XSetLocaleModifiers("@im=fcitx");
    Display *d = XOpenDisplay(NULL);
    if (!d) { printf("FT no_display\n"); return 1; }
    printf("FT before_openim\n");
    XIM im = XOpenIM(d, NULL, NULL, NULL);
    printf("FT after_openim im=%p\n", (void *)im);
    if (!im) { printf("FT no_im\n"); return 2; }
    XIMStyles *styles = NULL;
    XGetIMValues(im, XNQueryInputStyle, &styles, NULL);
    XIMStyle style = 0;
    for (unsigned i = 0; styles && i < styles->count_styles; i++) {
        XIMStyle s = styles->supported_styles[i];
        if ((s & (XIMPreeditNothing | XIMStatusNothing)) == (XIMPreeditNothing | XIMStatusNothing)) { style = s; break; }
    }
    printf("FT styles=%d chosen=0x%lx\n", styles ? (int)styles->count_styles : 0, (unsigned long)style);
    Window w = XCreateSimpleWindow(d, DefaultRootWindow(d), 0, 0, 200, 100, 0, 0, 0);
    XSelectInput(d, w, KeyPressMask | KeyReleaseMask | FocusChangeMask | StructureNotifyMask | ExposureMask);
    XMapWindow(d, w);
    XEvent e;
    for (;;) { XNextEvent(d, &e); if (e.type == MapNotify) break; }
    XSetInputFocus(d, w, RevertToParent, CurrentTime);
    printf("FT before_createic\n");
    XIC ic = XCreateIC(im, XNInputStyle, style, XNClientWindow, w, XNFocusWindow, w, NULL);
    if (!ic) { printf("FT no_ic\n"); return 3; }
    XSetICFocus(ic); XFlush(d);
    printf("FT ic_ok focus=%lu\n", XGetInputFocus(d, &w, NULL), w);
    const char *seq = "wqiy vbg ";
    for (const char *p = seq; *p; p++) {
        KeySym ks = (*p == ' ') ? XK_space : XStringToKeysym((char[]){*p, 0});
        feed(d, w, ic, ks);
        while (XPending(d) > 0) { XEvent x; XNextEvent(d, &x); (void)XFilterEvent(&x, w); }
    }
    printf("FT OUT_UTF8=[%s]\n", out);
    printf("FT OUT_HEX=");
    for (size_t i = 0; i < outlen; i++) printf("%02x", (unsigned char)out[i]);
    printf("\n");
    return 0;
}
CEOF
mkdir -p /tmp/.X11-unix
env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe \
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-noudev.conf >"$W/xorg.log" 2>&1 &
waited=0; while [ ! -S /tmp/.X11-unix/X0 ] && [ $waited -lt 40 ]; do sleep 1; waited=$((waited+1)); done
export DISPLAY=:0
echo "IM7 xorg=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no) gcc=$(command -v gcc >/dev/null && echo yes || echo no) xlib=$([ -f /usr/include/X11/Xlib.h ] && echo yes || echo no)"
if ! command -v gcc >/dev/null 2>&1; then echo "IM7 no_gcc apt=$(tail -1 "$W/apt.log" | tr ' ' '_' | head -c 120)"; echo "DAILY_DONE=ok"; exit 0; fi
gcc -O2 -o "$W/imtype" "$W/imtype.c" -lX11 2>"$W/cc.err" && echo "IM7 compile=ok" || { echo "IM7 compile=fail $(head -3 "$W/cc.err" | tr '\n' '|')"; echo "DAILY_DONE=ok"; exit 0; }
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then DBUS_SESSION_BUS_ADDRESS=$(dbus-daemon --session --fork --print-address); export DBUS_SESSION_BUS_ADDRESS; fi
mkdir -p /root/.config/fcitx5
printf '[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=wbpy\n\n[Groups/0/Items/0]\nName=wbpy\nLayout=\n\n[GroupOrder]\n0=Default\n' >/root/.config/fcitx5/profile
export GTK_IM_MODULE=fcitx QT_IM_MODULE=fcitx XMODIFIERS=@im=fcitx
pkill -x fcitx5 2>/dev/null; sleep 1
fcitx5 -d --replace >"$W/fcitx.log" 2>&1
sleep 5
echo "IM7 fcitx alive=$(pgrep -c -x fcitx5) engine=[$(fcitx5-remote -n 2>&1)]"
fcitx5-remote -o 2>/dev/null; sleep 1
echo "IM7 after_o engine=[$(fcitx5-remote -n 2>&1)] xim=[$(xprop -root XIM_SERVERS 2>&1 | head -1)]"
timeout 15 "$W/imtype" 2>&1 | sed 's/^/IM7 /'
echo "IM7 done"
echo "DAILY_IM7=ok"; echo "DAILY_DONE=ok"
