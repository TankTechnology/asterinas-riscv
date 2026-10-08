#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M7: assemble the SCM_RIGHTS + SO_PEERCRED minimal repro as a static /init,
# pack a tiny initramfs, and repack the QEMU boot disk with the current kernel
# Image + this initramfs. Mirrors the M4 minimal-repro flow.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
BUILD_ROOT="${NIXOS_ROOT}/m7"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/m7-initramfs.cpio}"

KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"

CC_STATIC="riscv64-linux-gnu-gcc"

if ! command -v "${CC_STATIC}" >/dev/null 2>&1; then
    echo "missing ${CC_STATIC}; install riscv64-linux-gnu-gcc" >&2
    exit 2
fi

mkdir -p "${BUILD_ROOT}"

# 1. Static /init (glibc static, same pattern as M1-M6).
"${CC_STATIC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${BUILD_ROOT}/init" "${SRC_DIR}/scm_repro.c"

# 2. Assemble the rootfs. /dev must exist for the kernel's first-process stdio.
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"
cp "${BUILD_ROOT}/init" "${ROOTFS}/init"

# 3. Pack as newc cpio (uncompressed, like M3+).
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )

# 4. Repack the boot disk with the current kernel Image + this initramfs.
STAGE="${REPO_ROOT}/target/qemu-uboot/current/.m7-stage"
rm -rf "${STAGE}"; mkdir -p "${STAGE}"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
rm -f "${BOOT_DISK}"
truncate -s 96M "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"

echo "built ${OUTPUT}"
echo "  init: $(file -b "${BUILD_ROOT}/init" | cut -c1-60)"
echo "  boot disk: ${BOOT_DISK}"
