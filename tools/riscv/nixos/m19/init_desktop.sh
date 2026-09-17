#!/bin/busybox sh
# DRM-M19 desktop demo init: go straight to kmscube (no test harness).
/bin/busybox mount -t devtmpfs devtmpfs /dev 2>/dev/null
/bin/busybox mount -t proc proc /proc 2>/dev/null
/bin/busybox echo M19_DESKTOP_START
/bin/busybox ls -l /dev/dri
/usr/bin/kmscube -D /dev/dri/card0 &
exec /bin/busybox sh
