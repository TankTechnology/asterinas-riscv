#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the DRM-M3 initramfs: Xorg running the standard modesetting driver on
# /dev/dri/card0, plus a small static client that fills the root window with a
# gradient. Reuses the cross-compiled Xorg/module chain in the sibling tree
# (asterinas-riscv/target/riscv-cross), adding the modesetting driver and libdrm
# that were built for this milestone.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The sibling tree owns the xserver/libdrm cross-compile assets.
CROSS_USR="/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/usr"
SYSROOT_LIB="/usr/riscv64-linux-gnu/lib"

BUILD_ROOT="${REPO_ROOT}/target/nixos/drm-m3"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-/tmp/drm-m3/initramfs.cpio.gz}"

CC="riscv64-linux-gnu-gcc"

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/lib" "${ROOTFS}/usr/lib" "${ROOTFS}/usr/bin" \
    "${ROOTFS}/usr/lib/xorg/modules/drivers" \
    "${ROOTFS}/usr/lib/xorg/modules/input" "${ROOTFS}/etc" \
    "${ROOTFS}/usr/share/X11/xkb" "${ROOTFS}/bin" \
    "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp" \
    "${ROOTFS}/root"

# /init launcher (static).
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_m3.c"

# Static X11 draw client.
PKG_CONFIG_LIBDIR="${CROSS_USR}/lib/pkgconfig:${CROSS_USR}/share/pkgconfig" \
    "${CC}" -static -O2 -no-pie -fno-stack-protector \
    -o "${ROOTFS}/usr/bin/xfill" "${SRC_DIR}/xfill.c" \
    $(PKG_CONFIG_LIBDIR="${CROSS_USR}/lib/pkgconfig:${CROSS_USR}/share/pkgconfig" \
        pkg-config --static --cflags --libs x11)

# Dynamic linker + glibc runtime.
cp "${SYSROOT_LIB}/ld-linux-riscv64-lp64d.so.1" "${ROOTFS}/lib/"
for lib in libc.so.6 libm.so.6 libdl.so.2 librt.so.1 libpthread.so.0 libgcc_s.so.1; do
    cp "${SYSROOT_LIB}/${lib}" "${ROOTFS}/lib/"
done

# BusyBox as /bin/sh (Xorg compiles keymaps via popen("sh -c xkbcomp ...")).
if [ -f "${CROSS_USR}/bin/busybox" ]; then
    cp "${CROSS_USR}/bin/busybox" "${ROOTFS}/bin/busybox"
    ln -sf busybox "${ROOTFS}/bin/sh"
else
    echo "WARNING: busybox not found; Xorg keyboard init will fail" >&2
fi

# libxcvt (Xorg's only non-glibc dynamic dependency) + libdrm (modesetting).
cp "${CROSS_USR}/lib/libxcvt.so.0.1.3" "${ROOTFS}/usr/lib/libxcvt.so.0.1.3"
ln -sf libxcvt.so.0.1.3 "${ROOTFS}/usr/lib/libxcvt.so.0"
cp "${CROSS_USR}/lib/libdrm.so.2.4.0" "${ROOTFS}/usr/lib/libdrm.so.2.4.0"
ln -sf libdrm.so.2.4.0 "${ROOTFS}/usr/lib/libdrm.so.2"

# Xorg binary + loadable modules + the modesetting/evdev drivers.
cp "${CROSS_USR}/bin/Xorg" "${ROOTFS}/usr/bin/Xorg"
cp "${CROSS_USR}/lib/xorg/modules/"*.so "${ROOTFS}/usr/lib/xorg/modules/"
cp "/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/src/xserver/build/hw/xfree86/drivers/modesetting/modesetting_drv.so" \
    "${ROOTFS}/usr/lib/xorg/modules/drivers/"
cp "${CROSS_USR}/lib/xorg/modules/input/evdev_drv.so" "${ROOTFS}/usr/lib/xorg/modules/input/"

# xkbcomp (static) at its configure-time XKB_BIN_DIRECTORY, so Xorg's shell-out
# can find it. Path is baked into the sibling cross tree; place it identically.
XKBCOMP_BAKED="/home/arch-anjie/Program/asterinas-riscv/target/riscv-cross/usr/bin"
if [ -f "${CROSS_USR}/bin/xkbcomp" ]; then
    mkdir -p "${ROOTFS}${XKBCOMP_BAKED}"
    cp "${CROSS_USR}/bin/xkbcomp" "${ROOTFS}${XKBCOMP_BAKED}/xkbcomp"
else
    echo "WARNING: xkbcomp not found; Xorg keyboard init will fail" >&2
fi

# Config.
cp "${SRC_DIR}/xorg-modesetting.conf" "${ROOTFS}/etc/xorg.conf"

# xkeyboard-config data (XKB rules/symbols/...). evdev needs them to compile a
# keymap; without them the server aborts on the virtual core keyboard.
if [ -d "/usr/share/X11/xkb" ]; then
    cp -rL "/usr/share/X11/xkb/." "${ROOTFS}/usr/share/X11/xkb/"
else
    echo "WARNING: host XKB data not found; Xorg keyboard init will fail" >&2
fi

# Strip debug info from every ELF we ship. The cross-built modules and the
# statically-linked client carry large symbol tables; stripping keeps the
# initramfs small enough that its gzip decompression is fast under QEMU TCG.
find "${ROOTFS}" -type f \( -name '*.so*' -o -name Xorg -o -name xfill \
    -o -name busybox -o -name xkbcomp \) \
    | while read -r f; do riscv64-linux-gnu-strip "$f" 2>/dev/null || true; done

# Pack as newc cpio + gzip.
mkdir -p "$(dirname "${OUTPUT}")"
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
