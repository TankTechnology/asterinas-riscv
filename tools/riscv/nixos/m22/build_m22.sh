#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Builds the DRM-M22 resource-lifetime stress initramfs and boot disk.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/drm-m22"
ROOTFS="${BUILD_ROOT}/rootfs"
DISK_DIR="${REPO_ROOT}/target/drm-m22"
OUTPUT="${DISK_DIR}/initramfs.cpio.gz"
BOOT_DISK="${DISK_DIR}/boot.ext4"
U_BOOT="${DISK_DIR}/u-boot"
DTB="${DISK_DIR}/qemu-virt.dtb"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
BOOT_ASSET_DIR="${REPO_ROOT}/target/drm-m16"
CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

if ! command -v "${CC}" >/dev/null 2>&1; then
    echo "missing ${CC}" >&2
    exit 2
fi
if [[ ! -f "${KERNEL_IMAGE}" ]]; then
    echo "missing kernel image ${KERNEL_IMAGE}" >&2
    exit 2
fi

mkdir -p "${DISK_DIR}"
if [[ ! -f "${U_BOOT}" ]]; then
    cp "${BOOT_ASSET_DIR}/u-boot" "${U_BOOT}"
fi
if [[ ! -f "${DTB}" ]]; then
    cp "${BOOT_ASSET_DIR}/qemu-virt.dtb" "${DTB}" 2>/dev/null || \
        cp "${BOOT_ASSET_DIR}/qemu-virt-smp4.dtb" "${DTB}"
fi

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}"
"${CC}" -O2 -static -no-pie -fno-stack-protector -Wall -Wextra -Werror \
    -o "${ROOTFS}/init" "${SRC_DIR}/resource_stress.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

STAGE="$(mktemp -d)"
trap 'rm -rf -- "${STAGE}"' EXIT
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
rm -f "${BOOT_DISK}"
INITRD_BYTES=$(stat -c%s "${OUTPUT}")
KERNEL_BYTES=$(stat -c%s "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 32*1024*1024) / 1024 / 1024 + 1 ))
if (( BOOT_MB < 96 )); then
    BOOT_MB=96
fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf -- "${STAGE}"
trap - EXIT

echo "built ${OUTPUT}"
echo "packed ${BOOT_DISK} (${BOOT_MB}M)"
