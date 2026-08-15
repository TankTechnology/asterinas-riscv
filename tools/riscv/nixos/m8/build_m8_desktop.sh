#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DRM-M8 (part 2): assemble the DRM systemd-desktop initramfs and re-pack the
# independent /tmp/drm-m8-desktop boot disk with the DRM-tree kernel.
#
# This promotes the M5 integration to the main desktop boot chain: the guest
# runs the sibling asterinas-riscv tree's systemd desktop (Xorg + matchbox-wm +
# xpanel + pcmanfm + xterm + NetSurf), but with **both** the DRM modesetting
# driver (primary, /dev/dri/card0) and the bochs fbdev driver (fallback,
# /dev/fb0) bundled. /init selects the Xorg config at runtime by probing for
# /dev/dri/card0, so the same rootfs boots either GPU.
#
#   /tmp/drm-m8-desktop/boot.ext4  (kernel + initramfs + dtb, ext4)
#
# Usage: bash build_m8_desktop.sh [--no-repack]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The systemd desktop userspace + the DRM modesetting driver + libdrm live in
# the sibling asterinas-riscv tree (built by its build_systemd_desktop.sh and the
# DRM-M3 cross-compile).
DESKTOP_TREE="${DESKTOP_TREE:-$HOME/Program/asterinas-riscv}"
DESKTOP_ROOTFS="${DESKTOP_TREE}/target/systemd-desktop/rootfs"
CROSS_USR="${DESKTOP_TREE}/target/riscv-cross/usr"
MODESETTING_SO="${DESKTOP_TREE}/target/riscv-cross/src/xserver/build/hw/xfree86/drivers/modesetting/modesetting_drv.so"

BUILD_ROOT="${REPO_ROOT}/target/nixos/m8"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${BUILD_ROOT}/initramfs.cpio"

DISK_DIR="/tmp/drm-m8-desktop"
BOOT_DISK="${DISK_DIR}/boot.ext4"
U_BOOT="${DISK_DIR}/u-boot"
DTB="${DISK_DIR}/qemu-virt.dtb"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

NO_REPACK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-repack) NO_REPACK=1 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

[[ -d "${DESKTOP_ROOTFS}" ]] || { echo "missing desktop rootfs: ${DESKTOP_ROOTFS}" >&2; exit 2; }
[[ -f "${MODESETTING_SO}" ]] || { echo "missing modesetting driver: ${MODESETTING_SO}" >&2; exit 2; }
[[ -f "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image: ${KERNEL_IMAGE}" >&2; exit 2; }

mkdir -p "${DISK_DIR}"
[[ -f "${U_BOOT}" ]] || cp /tmp/drm-m5/u-boot "${U_BOOT}"
[[ -f "${DTB}" ]] || cp /tmp/drm-m5/qemu-virt.dtb "${DTB}"

echo "=== assembling DRM-M8 desktop rootfs ==="
mkdir -p "${BUILD_ROOT}"
rm -rf "${ROOTFS}"
cp -a "${DESKTOP_ROOTFS}" "${ROOTFS}"

# --- 1. DRM modesetting driver + libdrm (alongside the bundled fbdev) --------
mkdir -p "${ROOTFS}/usr/lib/xorg/modules/drivers"
cp "${MODESETTING_SO}" "${ROOTFS}/usr/lib/xorg/modules/drivers/modesetting_drv.so"
cp -a "${CROSS_USR}/lib/libdrm.so.2.4.0" "${ROOTFS}/usr/lib/libdrm.so.2.4.0"
ln -sf libdrm.so.2.4.0 "${ROOTFS}/usr/lib/libdrm.so.2"
ln -sf libdrm.so.2     "${ROOTFS}/usr/lib/libdrm.so"

# --- 2. Xorg config pair (runtime fallback via /init) ------------------------
cp "${SRC_DIR}/../drm/xorg-modesetting.conf" "${ROOTFS}/etc/xorg-modesetting.conf"
cp "${SRC_DIR}/xorg-fbdev.conf" "${ROOTFS}/etc/xorg-fbdev.conf"

# --- 3. /init launcher with GPU fallback -------------------------------------
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_drm.c"

# --- 4. pack raw newc cpio (no gzip) -----------------------------------------
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )
echo "built ${OUTPUT}"

if [[ "${NO_REPACK}" -eq 1 ]]; then
    echo "assembled rootfs (--no-repack): ${ROOTFS}"
    exit 0
fi

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
rm -f "${BOOT_DISK}"
INITRD_BYTES=$(stat -c%s "${OUTPUT}")
KERNEL_BYTES=$(stat -c%s "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 32*1024*1024) / 1024 / 1024 + 1 ))
FLOOR_MB=128
if (( BOOT_MB < FLOOR_MB )); then BOOT_MB=${FLOOR_MB}; fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "re-packed ${BOOT_DISK} (${BOOT_MB}M)"
