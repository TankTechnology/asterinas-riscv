#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Run inside the Asterinas guest.  Copies the staged vendor UMD bundle into
# /opt/pvr (or uses the source directly), then makes the first EGL contact.
#
# Usage: sh p5b-guest.sh [/home/debian/pvr | /tmp/pvr/pvr]

set -eu
SRC=${1:-/home/debian/pvr}
DRI_FALLBACK=/usr/lib/riscv64-linux-gnu/dri

if ! command -v timeout >/dev/null 2>&1; then
    echo "ASTERINAS_POWERVR_UMD status=failed stage=egl reason=no-timeout"
    exit 2
fi
if [ ! -x "$SRC/bin/eglconfigs" ]; then
    echo "PVR_BUNDLE_MISSING $SRC"
    exit 2
fi

if [ "$SRC" != "/opt/pvr" ]; then
    if [ -d /opt/pvr ] && [ ! -e /opt/pvr.p5b.bak ]; then
        cp -r /opt/pvr /opt/pvr.p5b.bak
    fi
    mkdir -p /opt/pvr
    cp -r "$SRC/." /opt/pvr/
fi

echo "PVR_BUNDLE_PRESENT"
ls -l /opt/pvr/bin/eglconfigs /opt/pvr/dri/pvr_dri.so /opt/pvr/lib/libsrv_um.so

export LD_LIBRARY_PATH=/opt/pvr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export LIBGL_DRIVERS_PATH=/opt/pvr/dri:$DRI_FALLBACK
export MESA_LOADER_DRIVER_OVERRIDE=pvr

for platform in surfaceless device gbm; do
    EGL_PLATFORM=$platform timeout 30 /opt/pvr/bin/eglconfigs >/tmp/egl.out 2>&1 || rc=$?
    rc=${rc:-0}
    echo "EGL_PLATFORM=$platform EGL_RC=$rc"
    head -60 /tmp/egl.out || true
    if [ "$rc" -eq 0 ]; then
        echo "ASTERINAS_POWERVR_UMD status=ok stage=egl platform=$platform libsrv=24.2.6643903"
        exit 0
    fi
    unset rc
done

echo "ASTERINAS_POWERVR_UMD status=failed stage=egl libsrv=24.2.6643903"
exit 1
