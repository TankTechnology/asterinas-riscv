#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# P6 skeleton: start Firefox against the P5c Xorg display and collect logs.
# The about:support / WebRender marker parsing is TODO until the board is back.

set -u
DISPLAY=${DISPLAY:-:99}
DRI_FALLBACK=/usr/lib/riscv64-linux-gnu/dri
export DISPLAY

if [ ! -S /tmp/.X11-unix/X99 ]; then
    echo "ASTERINAS_POWERVR_WEBRENDER status=failed reason=no-xorg"
    exit 2
fi
if ! command -v firefox >/dev/null 2>&1; then
    echo "ASTERINAS_POWERVR_WEBRENDER status=failed reason=no-firefox"
    exit 2
fi
if ! command -v timeout >/dev/null 2>&1; then
    echo "ASTERINAS_POWERVR_WEBRENDER status=failed reason=no-timeout"
    exit 2
fi

export LD_LIBRARY_PATH=/opt/pvr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export LIBGL_DRIVERS_PATH=/opt/pvr/dri:$DRI_FALLBACK
export MESA_LOADER_DRIVER_OVERRIDE=pvr
export MOZ_X11_EGL=1

PROFILE=/tmp/p6-profile
mkdir -p "$PROFILE"
cat >"$PROFILE/user.js" <<'EOF'
user_pref("gfx.webrender.all", true);
user_pref("gfx.webrender.software", false);
user_pref("gfx.webrender.force-disabled", false);
user_pref("gfx.webrender.fallback.software", false);
EOF

# TODO(P6): replace `about:support` scraping with a Marionette/remote
# session that returns the compositing and WebRender backend fields.
timeout 30 firefox --profile "$PROFILE" about:support >/tmp/p6-firefox.log 2>&1 &
pid=$!
sleep 10

if ! kill -0 "$pid" 2>/dev/null; then
    echo "ASTERINAS_POWERVR_WEBRENDER status=failed reason=firefox-start"
    echo "P6_FIREFOX_LOG=/tmp/p6-firefox.log"
    exit 3
fi

echo "ASTERINAS_POWERVR_WEBRENDER status=failed reason=todo-about-support-unparsed"
echo "P6_FIREFOX_LOG=/tmp/p6-firefox.log"
kill "$pid" 2>/dev/null || true
sleep 1
kill -9 "$pid" 2>/dev/null || true
exit 4