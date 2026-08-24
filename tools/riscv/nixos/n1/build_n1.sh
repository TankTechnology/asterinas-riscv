#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# NIXOS-N1: build the netlink-probe initramfs and pack a private boot disk.
#
# The boot disk goes to /tmp/n1-netlink/boot.ext4 (never the shared
# target/qemu-uboot/current/boot.ext4) and contains the current kernel Image,
# the N1 initramfs, and the QEMU virt DTB.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos"
BUSYBOX="${BUILD_ROOT}/busybox"
ROOTFS="${BUILD_ROOT}/n1-rootfs"
INITRAMFS="${BUILD_ROOT}/n1-initramfs.cpio.gz"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"

DISK_DIR="${N1_DISK_DIR:-/tmp/n1-netlink}"
BOOT_DISK="${DISK_DIR}/boot.ext4"

CC="riscv64-linux-gnu-gcc"

[[ -x "${BUSYBOX}" ]] || { echo "missing ${BUSYBOX}; run build_busybox.sh first" >&2; exit 2; }
[[ -s "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -s "${DTB}" ]] || { echo "missing DTB: ${DTB}" >&2; exit 2; }

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/bin" "${ROOTFS}/dev" "${ROOTFS}/proc" \
    "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector -o "${ROOTFS}/init" "${SRC_DIR}/init_n1.c"
"${CC}" -O2 -static -o "${ROOTFS}/bin/nlprobe" "${SRC_DIR}/nlprobe.c"

cp "${BUSYBOX}" "${ROOTFS}/bin/busybox"
for applet in sh ip echo mount cat; do
    ln -sf busybox "${ROOTFS}/bin/${applet}"
done

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${INITRAMFS}" )
echo "built ${INITRAMFS}"

mkdir -p "${DISK_DIR}"
STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${INITRAMFS}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
INITRD_BYTES=$(wc -c < "${INITRAMFS}")
KERNEL_BYTES=$(wc -c < "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 64*1024*1024) / 1024 / 1024 + 1 ))
if (( BOOT_MB < 128 )); then BOOT_MB=128; fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "packed ${BOOT_DISK} (${BOOT_MB}M)"
