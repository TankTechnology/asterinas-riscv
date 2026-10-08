#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the ET_EXEC + PT_INTERP repro initramfs and re-pack the boot disk.
#
# Cross-compiles a non-PIE dynamically linked /hello (glibc), packages it with
# the riscv64 glibc dynamic linker + libc, then re-packs boot.ext4. Run
# boot_etexec_smoke.py afterwards.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
ROOTFS="${NIXOS_ROOT}/etexec/rootfs"
OUTPUT="${NIXOS_ROOT}/etexec/etexec-initramfs.cpio.gz"

BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"

CC="riscv64-linux-gnu-gcc"
SYSROOT="$(dirname "$(command -v "${CC}")")/../riscv64-linux-gnu"

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

GLIBC_LIB="${SYSROOT}/lib"
if [[ ! -e "${GLIBC_LIB}/ld-linux-riscv64-lp64d.so.1" ]]; then
    echo "missing glibc loader under ${GLIBC_LIB}" >&2; exit 2
fi

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/bin" "${ROOTFS}/dev" "${ROOTFS}/proc" \
    "${ROOTFS}/sys" "${ROOTFS}/tmp" "${ROOTFS}/lib"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_etexec.c"
"${CC}" -O2 -no-pie -fno-stack-protector \
    -o "${ROOTFS}/hello" "${SRC_DIR}/hello_etexec.c"

cp -P "${GLIBC_LIB}/ld-linux-riscv64-lp64d.so.1" "${ROOTFS}/lib/"
cp -P "${GLIBC_LIB}/libc.so.6" "${ROOTFS}/lib/"

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
