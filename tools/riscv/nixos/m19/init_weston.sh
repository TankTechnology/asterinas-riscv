#!/bin/busybox sh
# DRM-M19 desktop demo init: Weston on the DRM backend (real desktop).
/bin/busybox mount -t devtmpfs devtmpfs /dev 2>/dev/null
/bin/busybox mount -t proc proc /proc 2>/dev/null
/bin/busybox mkdir -p /sys /run/xdg /tmp
/bin/busybox mount -t sysfs sysfs /sys 2>/dev/null
/bin/busybox mount -t tmpfs tmpfs /run 2>/dev/null
/bin/busybox mkdir -p /run/xdg
/bin/busybox chmod 0700 /run/xdg
/bin/busybox echo M19_WESTON_START

# udev for input device discovery (libinput)
/usr/lib/systemd/systemd-udevd --daemon 2>/dev/null
/usr/bin/udevadm trigger --type=devices --action=add 2>/dev/null
/usr/bin/udevadm settle 2>/dev/null

export XDG_RUNTIME_DIR=/run/xdg
export LIBSEAT_BACKEND=builtin
export EGL_LOG_LEVEL=debug
/bin/busybox echo M19_INPUT_DEVICES
/bin/busybox ls -l /dev/input/ 2>&1

# Weston on the DRM backend; serial stays free for a shell.
/usr/bin/weston --backend=drm --shell=desktop --log=/run/weston.log &
exec /bin/busybox sh
