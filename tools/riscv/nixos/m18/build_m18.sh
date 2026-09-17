#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the DRM-M18 page-flip-event test initramfs and pack a fresh
# boot disk. The in-guest init is `flipevent` (statically linked, raw
# ioctls) which prints M18_* evidence lines on the serial console.
#
# Usage:
#     bash build_m18.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/drm-m18"
ROOTFS="${BUILD_ROOT}/rootfs"

DISK_DIR="${REPO_ROOT}/target/drm-m18"
OUTPUT="${DISK_DIR}/initramfs.cpio.gz"
BOOT_DISK="${DISK_DIR}/boot.ext4"
U_BOOT="${DISK_DIR}/u-boot"
DTB="${DISK_DIR}/qemu-virt.dtb"

KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
# u-boot + DTB are byte-identical across the DRM milestones.
SRC_DISK="${REPO_ROOT}/target/drm-m16"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

if ! command -v "${CC}" >/dev/null 2>&1; then
    echo "missing ${CC}" >&2; exit 2
fi

mkdir -p "${DISK_DIR}"

if [[ ! -f "${U_BOOT}" && -f "${SRC_DISK}/u-boot" ]]; then
    cp "${SRC_DISK}/u-boot" "${U_BOOT}"
fi
if [[ ! -f "${DTB}" ]]; then
    cp "${SRC_DISK}/qemu-virt.dtb" "${DTB}" 2>/dev/null || \
        cp "${SRC_DISK}/qemu-virt-smp4.dtb" "${DTB}"
fi

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/flipevent.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )
echo "built ${OUTPUT}"

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
rm -f "${BOOT_DISK}"
INITRD_BYTES=$(stat -c%s "${OUTPUT}")
KERNEL_BYTES=$(stat -c%s "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 32*1024*1024) / 1024 / 1024 + 1 ))
FLOOR_MB=96
if (( BOOT_MB < FLOOR_MB )); then BOOT_MB=${FLOOR_MB}; fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "packed ${BOOT_DISK} (${BOOT_MB}M)"
