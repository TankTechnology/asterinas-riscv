#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the mlock/munlock smoke-test initramfs and re-pack the boot disk.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
ROOTFS="${NIXOS_ROOT}/mlock/rootfs"
OUTPUT="${NIXOS_ROOT}/mlock/mlock-initramfs.cpio.gz"

BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"

CC="riscv64-linux-gnu-gcc"
NO_REPACK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-repack) NO_REPACK=1 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/bin" "${ROOTFS}/dev" "${ROOTFS}/proc" \
    "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_mlock.c"
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/mlock_smoke" "${SRC_DIR}/mlock_smoke.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )
echo "built ${OUTPUT}"

if [[ "${NO_REPACK}" -eq 0 ]]; then
    STAGE="$(mktemp -d)"
    cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
    cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
    cp "${DTB}" "${STAGE}/qemu-virt.dtb"
    rm -f "${BOOT_DISK}"
    truncate -s 96M "${BOOT_DISK}"
    mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
    rm -rf "${STAGE}"
    echo "re-packed ${BOOT_DISK}"
fi
