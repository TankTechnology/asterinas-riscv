#!/bin/bash
# SPDX-License-Identifier: MPL-2.0

# Guest-side Xorg acceptance for the shared DRM core.
#
# Runs as root from `xorg-gate.service`: starts the X server on /dev/dri/card0
# with input auto-detection disabled, paints the root window through the raw
# X11 client, and leaves the scanout on screen for the host screendump.
# Every step writes a marker to the systemd console, so the host can wait on
# them without a shell.

export DISPLAY=:0
echo XORG_GATE_BEGIN

if [ ! -e /dev/dri/card0 ]; then
    echo XORG_GATE_NO_DEVICE
    exit 1
fi

mkdir -p /tmp/.X11-unix
/usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
    -config /etc/X11/xorg-noudev.conf >/var/log/xorg-gate.log 2>&1 &
xorg_pid=$!

waited=0
while [ ! -S /tmp/.X11-unix/X0 ] && [ "$waited" -lt 60 ]; do
    sleep 1
    waited=$((waited + 1))
done
echo XORG_GATE_SOCKET_WAIT=$waited
if [ ! -S /tmp/.X11-unix/X0 ]; then
    echo XORG_GATE_SERVER_FAILED
    tail -n 20 /var/log/xorg-gate.log
    kill "$xorg_pid" 2>/dev/null
    exit 1
fi

# Report which presentation extensions the server exposes on this device, and
# what Mesa reports for GLX, so the DRI3 half of PRIME is observable from the
# serial console.
if command -v xdpyinfo >/dev/null 2>&1; then
    echo XORG_GATE_EXTENSIONS
    xdpyinfo -queryExtensions 2>/dev/null | grep -E "DRI3|DRI2|PRESENT|GLX" || true
fi
if command -v glxinfo >/dev/null 2>&1; then
    echo XORG_GATE_GLXINFO
    glxinfo -B 2>/dev/null | grep -E "renderer|OpenGL version|direct rendering" || true
fi

if /usr/local/bin/x11-paint 0; then
    echo XORG_GATE_PAINT=0
else
    paint_status=$?
    echo XORG_GATE_PAINT=$paint_status
    echo XORG_GATE_XORG_STATE
    ps -o pid,stat,utime,stime,args -p "$xorg_pid" 2>&1
    echo XORG_GATE_XORG_LOG
    tail -n 40 /var/log/xorg-gate.log
fi

# Keep the server (and the painted root window) alive long enough for the
# host to capture the scanout.
sleep 30
echo XORG_GATE_END
