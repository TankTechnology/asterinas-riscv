#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build an initramfs that launches Xorg (fbdev + evdev) on the Asterinas RISC-V
# framebuffer chain. Assumes the xorg-server and driver dependency chain has
# been cross-compiled into target/riscv-cross/usr (see build_wayland_deps.sh
# pattern; the Xorg chain is built manually via the xorg source trees).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CROSS_USR="${REPO_ROOT}/target/riscv-cross/usr"
SYSROOT_LIB="/usr/riscv64-linux-gnu/lib"
OUTPUT="${1:-${REPO_ROOT}/target/qemu-uboot/initramfs-xorg.cpio.gz}"
ROOTFS="${REPO_ROOT}/target/xorg-rootfs"

CC="riscv64-linux-gnu-gcc"

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/lib" "${ROOTFS}/usr/lib" "${ROOTFS}/usr/bin" \
    "${ROOTFS}/usr/lib/xorg/modules/drivers" \
    "${ROOTFS}/usr/lib/xorg/modules/input" "${ROOTFS}/etc" \
    "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

# /init launcher (static).
"${CC}" -O2 -static -no-pie -o "${ROOTFS}/init" "${SRC_DIR}/init.c"

# Dynamic linker + glibc runtime.
cp "${SYSROOT_LIB}/ld-linux-riscv64-lp64d.so.1" "${ROOTFS}/lib/"
for lib in libc.so.6 libm.so.6 libdl.so.2 librt.so.1 libpthread.so.0 libgcc_s.so.1; do
    cp "${SYSROOT_LIB}/${lib}" "${ROOTFS}/lib/"
done

# libxcvt (Xorg's only non-glibc dynamic dependency).
cp "${CROSS_USR}/lib/libxcvt.so.0.1.3" "${ROOTFS}/usr/lib/libxcvt.so.0.1.3"
ln -sf libxcvt.so.0.1.3 "${ROOTFS}/usr/lib/libxcvt.so.0"

# Xorg binary and drivers.
cp "${CROSS_USR}/bin/Xorg" "${ROOTFS}/usr/bin/Xorg"
cp "${CROSS_USR}/lib/xorg/modules/drivers/fbdev_drv.so" "${ROOTFS}/usr/lib/xorg/modules/drivers/"
cp "${CROSS_USR}/lib/xorg/modules/input/evdev_drv.so" "${ROOTFS}/usr/lib/xorg/modules/input/"

# Config.
cp "${SRC_DIR}/xorg.conf" "${ROOTFS}/etc/xorg.conf"

# Pack as newc cpio + gzip.
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
