#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the DRM-M4 cursor initramfs and re-pack the independent /tmp boot
# disk. The cursor smoke test drives the hardware cursor through the legacy
# MODE_CURSOR/MODE_CURSOR2 ioctls; the host verifies it against QEMU's
# `virtio_gpu_update_cursor` trace (the cursor overlay is not part of the
# console screendump).
#
# Reuses the /tmp/drm-m3 disk's u-boot + DTB (identical across the DRM
# milestones) and swaps in the freshly built kernel + cursor initramfs.
#
# Usage:
#     bash build_drm_m4.sh [--no-repack]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/drm-m4"
ROOTFS="${BUILD_ROOT}/rootfs"

DISK_DIR="/tmp/drm-m4"
OUTPUT="${DISK_DIR}/initramfs.cpio.gz"
BOOT_DISK="${DISK_DIR}/boot.ext4"
U_BOOT="${DISK_DIR}/u-boot"
DTB="${DISK_DIR}/qemu-virt.dtb"

KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
# Prior milestone's disk (u-boot + DTB are byte-identical across DRM-M1..M3;
# M1 keeps both at the top level, M3 keeps the DTB under staging/).
SRC_DISK="/tmp/drm-m1"
SRC_DTB="${SRC_DISK}/qemu-virt.dtb"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
NO_REPACK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-repack) NO_REPACK=1 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if ! command -v "${CC}" >/dev/null 2>&1; then
    echo "missing ${CC}" >&2; exit 2
fi

mkdir -p "${DISK_DIR}"

# Seed u-boot + DTB from the prior disk if this is a fresh /tmp.
if [[ ! -f "${U_BOOT}" && -f "${SRC_DISK}/u-boot" ]]; then
    cp "${SRC_DISK}/u-boot" "${U_BOOT}"
fi
if [[ ! -f "${DTB}" ]]; then
    if [[ -f "${SRC_DTB}" ]]; then
        cp "${SRC_DTB}" "${DTB}"
    elif [[ -f "/tmp/drm-m3/staging/qemu-virt.dtb" ]]; then
        cp "/tmp/drm-m3/staging/qemu-virt.dtb" "${DTB}"
    else
        echo "missing DTB: ${SRC_DTB}" >&2; exit 2
    fi
fi

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_m4.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )
echo "built ${OUTPUT}"

if [[ "${NO_REPACK}" -eq 0 ]]; then
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
    echo "re-packed ${BOOT_DISK} (${BOOT_MB}M)"
fi
