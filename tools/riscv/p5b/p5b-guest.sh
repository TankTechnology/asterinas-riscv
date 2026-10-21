#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Run inside the Asterinas guest.  Copies the staged vendor UMD bundle into
# /opt/pvr (or uses the source directly), then makes the first EGL contact.
#
# Usage: sh p5b-guest.sh [/home/debian/pvr | /tmp/pvr/pvr]

set -u
SRC=${1:-/home/debian/pvr}
if [ ! -x "$SRC/bin/eglconfigs" ]; then
    echo "PVR_BUNDLE_MISSING $SRC"
    exit 2
fi

mkdir -p /opt/pvr
cp -r "$SRC/." /opt/pvr/

echo "PVR_BUNDLE_PRESENT"
ls -l /opt/pvr/bin/eglconfigs /opt/pvr/dri/pvr_dri.so /opt/pvr/lib/libsrv_um.so || exit 3

export LD_LIBRARY_PATH=/opt/pvr/lib
export LIBGL_DRIVERS_PATH=/opt/pvr/dri
export MESA_LOADER_DRIVER_OVERRIDE=pvr

for platform in surfaceless device gbm; do
    export EGL_PLATFORM=$platform
    timeout 30 /opt/pvr/bin/eglconfigs >/tmp/egl.out 2>&1
    rc=$?
    echo "EGL_PLATFORM=$platform EGL_RC=$rc"
    head -60 /tmp/egl.out
    if [ "$rc" -eq 0 ]; then
        echo "ASTERINAS_POWERVR_UMD status=ok stage=egl platform=$platform libsrv=24.2.6643903"
        exit 0
    fi
done

echo "ASTERINAS_POWERVR_UMD status=failed stage=egl libsrv=24.2.6643903"
exit 1