#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DRM-M7: assemble the persistent-storage smoke-test boot disk + ext2 data disk
# under /tmp/drm-m7. Reuses the DRM-tree kernel (already built with the
# virtio-mmio enumeration fix) and the M5 U-Boot + DTB.
#
#   boot disk  : /tmp/drm-m7/boot.ext4   (kernel + /init + dtb, ext4)
#   data disk  : /tmp/drm-m7/nix-store.ext2 (256 MiB ext2, 4096-byte blocks)
#
# The data disk is created only if absent (it persists across the two boots that
# boot_m7.py performs).
#
# Usage: bash build_m7.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="/tmp/drm-m7"
M5="/tmp/drm-m5"
CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"

mkdir -p "${OUT}"

# 1. cross-compile the persistence /init (static glibc)
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${OUT}/persist-init" "${SRC_DIR}/persist_init.c"

# 2. minimal initramfs (raw newc cpio). The kernel mounts devtmpfs at /dev and
#    needs /dev to exist (device::init_in_first_process looks it up); /home is
#    the mount point for the ext2 data disk.
STAGE="$(mktemp -d)"
mkdir -p "${STAGE}/initroot/dev" "${STAGE}/initroot/proc" "${STAGE}/initroot/sys" \
         "${STAGE}/initroot/tmp" "${STAGE}/initroot/home"
cp "${OUT}/persist-init" "${STAGE}/initroot/init"
( cd "${STAGE}/initroot" && find . | cpio -o -H newc 2>/dev/null > "${OUT}/initramfs.cpio" )
rm -rf "${STAGE}"

# 3. ext2 data disk (4096-byte blocks: the driver's only supported size)
if [[ ! -s "${OUT}/nix-store.ext2" ]]; then
    truncate -s 256M "${OUT}/nix-store.ext2"
    mkfs.ext2 -q -F -b 4096 "${OUT}/nix-store.ext2"
fi

# 4. boot disk: kernel + initramfs + dtb (+ u-boot seed from M5)
[[ -f "${KERNEL_IMAGE}" ]] || { echo "missing kernel image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -f "${OUT}/u-boot" ]] || cp "${M5}/u-boot" "${OUT}/u-boot"
[[ -f "${OUT}/qemu-virt.dtb" ]] || cp "${M5}/qemu-virt.dtb" "${OUT}/qemu-virt.dtb"

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUT}/initramfs.cpio" "${STAGE}/initramfs.cpio"
cp "${OUT}/qemu-virt.dtb" "${STAGE}/qemu-virt.dtb"
rm -f "${OUT}/boot.ext4"
truncate -s 128M "${OUT}/boot.ext4"
mkfs.ext4 -q -F -d "${STAGE}" "${OUT}/boot.ext4"
rm -rf "${STAGE}"

echo "built DRM-M7 boot disk + data disk under ${OUT}"
