#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Run inside the Asterinas guest: install the P5c increment, do an EGL sanity
# check, then start Xorg with modesetting + glamor on /dev/dri/card0.
#
# Usage: sh p5c-guest.sh [/home/debian/pvr-p5c | /tmp/pvr-p5c]

SRC=${1:-/home/debian/pvr-p5c}

if [ ! -x "$SRC/opt/pvr/bin/eglconfigs" ]; then
    echo "ASTERINAS_POWERVR_XORG status=failed reason=missing-bundle"
    exit 2
fi

mkdir -p /opt/pvr /etc/X11/xorg.conf.d
cp -r "$SRC/opt/pvr/." /opt/pvr/
cp -r "$SRC/etc/." /etc/ 2>/dev/null || true

if [ ! -x /opt/pvr/bin/eglconfigs ] || [ ! -f /opt/pvr/dri/pvr_dri.so ] ||
   [ ! -f /opt/pvr/lib/libsrv_um.so ]; then
    echo "ASTERINAS_POWERVR_XORG status=failed reason=missing-lib"
    exit 3
fi

echo "PVR_BUNDLE_PRESENT"
ls -l /opt/pvr/bin/eglconfigs /opt/pvr/dri/pvr_dri.so /opt/pvr/lib/libsrv_um.so
ls -l /dev/dri/card0 /dev/dri/renderD128 2>/dev/null || {
    echo "ASTERINAS_POWERVR_XORG status=failed reason=no-drm-node"
    exit 4
}

export LD_LIBRARY_PATH=/opt/pvr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export LIBGL_DRIVERS_PATH=/opt/pvr/dri
export MESA_LOADER_DRIVER_OVERRIDE=pvr

# EGL sanity first (non-fatal; Xorg is the real gate).
EGL_PLATFORM=gbm timeout 20 /opt/pvr/bin/eglconfigs >/tmp/p5c-egl.out 2>&1
echo "P5C_EGL_RC=$?"
head -40 /tmp/p5c-egl.out

cat >/tmp/p5c-xorg.conf <<'EOF'
Section "ServerLayout"
	Identifier "P5cLayout"
	Screen 0 "P5cScreen"
EndSection

Section "Screen"
	Identifier "P5cScreen"
	Device "P5cDevice"
EndSection

Section "Device"
	Identifier "P5cDevice"
	Driver "modesetting"
	Option "kmsdev" "/dev/dri/card0"
	Option "AccelMethod" "glamor"
	Option "UseGammaLUT" "false"
EndSection
EOF

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null
Xorg :99 -config /tmp/p5c-xorg.conf -logfile /tmp/xorg-pvr.log -noreset >/tmp/xorg-stdout.log 2>&1 &
xpid=$!

ready=0
i=0
while [ "$i" -lt 20 ]; do
    if [ -S /tmp/.X11-unix/X99 ]; then ready=1; break; fi
    if ! kill -0 "$xpid" 2>/dev/null; then break; fi
    sleep 0.5
    i=$((i + 1))
done

echo "P5C_XORG_READY=$ready"
grep -Ei "glamor|egl|pvr|modeset|fatal|error" /tmp/xorg-pvr.log 2>/dev/null | head -80

if [ "$ready" -ne 1 ]; then
    echo "ASTERINAS_POWERVR_XORG status=failed reason=xorg-start"
    kill "$xpid" 2>/dev/null
    exit 5
fi

if grep -Eiq "glamor.*(enabled|EGL)|EGL.*glamor" /tmp/xorg-pvr.log 2>/dev/null; then
    echo "ASTERINAS_POWERVR_XORG status=ok backend=glamor display=:99"
    echo "P5C_XORG_LOG=/tmp/xorg-pvr.log"
    kill "$xpid" 2>/dev/null
    exit 0
fi

echo "ASTERINAS_POWERVR_XORG status=failed reason=glamor-not-egl"
echo "P5C_XORG_LOG=/tmp/xorg-pvr.log"
kill "$xpid" 2>/dev/null
exit 6