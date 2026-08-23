#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# NIXOS-N5: assemble (1) a persistent ext2 root disk (systemd tree + nix
# closure) and (2) a stage-1 initramfs, then pack a private boot disk.
#
# Disks (both under ${N5_DISK_DIR:-/tmp/n5}):
#   boot.ext4  — kernel Image + stage1 initramfs + DTB (repacked every run)
#   root.ext2  — PERSISTENT root filesystem (created once, reused across
#                boots so profile/store state survives a reboot)
#
# The cpio is uncompressed (zune-inflate hangs on >16 MB gzip, M3-report.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos"
N3_ROOT="${BUILD_ROOT}/n3"
SD_ROOTFS="${BUILD_ROOT}/systemd/rootfs"
N5_ROOT="${BUILD_ROOT}/n5"
INITRAMFS="${N5_ROOT}/n5-initramfs.cpio"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"

DISK_DIR="${N5_DISK_DIR:-/tmp/n5}"
BOOT_DISK="${DISK_DIR}/boot.ext4"
ROOT_DISK="${DISK_DIR}/root.ext2"

CC="riscv64-linux-gnu-gcc"

[[ -d "${SD_ROOTFS}" ]] || { echo "missing systemd rootfs: ${SD_ROOTFS}" >&2; exit 2; }
[[ -d "${N3_ROOT}/rootfs/nix/store" ]] || { echo "missing n3 rootfs (run n3/build_n3.sh first)" >&2; exit 2; }
[[ -s "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image" >&2; exit 2; }
[[ -s "${DTB}" ]] || { echo "missing DTB" >&2; exit 2; }

mkdir -p "${N5_ROOT}" "${DISK_DIR}"

# 1. Persistent root disk: systemd tree + the nix closure. Created once;
#    later runs keep the on-disk state (that is the point of R1-B).
if [[ ! -f "${ROOT_DISK}" ]]; then
    echo "=== creating persistent root disk ${ROOT_DISK} ==="
    STAGE="$(mktemp -d)"
    cp -a "${SD_ROOTFS}/." "${STAGE}/"
    mkdir -p "${STAGE}/nix"
    cp -a "${N3_ROOT}/rootfs/nix/store" "${STAGE}/nix/store"
    cp "${N3_ROOT}/rootfs/nix/.reginfo" "${STAGE}/nix/.reginfo"
    mkdir -p "${STAGE}/etc/nix"
    cp "${N3_ROOT}/rootfs/etc/nix/nix.conf" "${STAGE}/etc/nix/nix.conf"
    cp "${N3_ROOT}/rootfs/etc/nsswitch.conf" "${STAGE}/etc/nsswitch.conf"
    cp "${N3_ROOT}/rootfs/etc/resolv.conf" "${STAGE}/etc/resolv.conf"
    truncate -s 512M "${ROOT_DISK}"
    mkfs.ext2 -q -F -d "${STAGE}" "${ROOT_DISK}"
    rm -rf "${STAGE}"
    echo "created ${ROOT_DISK}"
else
    echo "reusing persistent root disk ${ROOT_DISK}"
fi

# 2. Stage-1 initramfs: just the static launcher (everything else is on disk).
S1="${N5_ROOT}/stage1-rootfs"
rm -rf "${S1}"
mkdir -p "${S1}/dev"
"${CC}" -O2 -static -no-pie -fno-stack-protector -o "${S1}/init" "${SRC_DIR}/init_stage1.c"
( cd "${S1}" && find . | cpio -o -H newc 2>/dev/null > "${INITRAMFS}" )
echo "built ${INITRAMFS}"

# 3. Boot disk (kernel + stage1 initramfs + DTB).
STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${INITRAMFS}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
truncate -s 64M "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "packed ${BOOT_DISK}"
