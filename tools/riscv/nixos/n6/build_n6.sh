#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# NIXOS-N6: build the tiny nsprobe initramfs and pack a private boot disk.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos"
N6_ROOT="${BUILD_ROOT}/n6"
ROOTFS="${N6_ROOT}/rootfs"
INITRAMFS="${N6_ROOT}/n6-initramfs.cpio"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"

DISK_DIR="${N6_DISK_DIR:-/tmp/n6-nsprobe}"
BOOT_DISK="${DISK_DIR}/boot.ext4"

CC="riscv64-linux-gnu-gcc"

mkdir -p "${N6_ROOT}" "${DISK_DIR}"
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/bin" "${ROOTFS}/dev"

"${CC}" -O2 -static -no-pie -fno-stack-protector -o "${ROOTFS}/init" "${SRC_DIR}/init_n6.c"
"${CC}" -O2 -static -o "${ROOTFS}/bin/nsprobe" "${SRC_DIR}/nsprobe.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${INITRAMFS}" )

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${INITRAMFS}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
truncate -s 64M "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "packed ${BOOT_DISK}"
