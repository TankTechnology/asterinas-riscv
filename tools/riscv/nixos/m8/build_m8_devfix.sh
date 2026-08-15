#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DRM-M8 (part 1): assemble the devtmpfs auto-create regression boot disk under
# /tmp/drm-m8-dev. The initramfs deliberately contains **no `/dev`** directory —
# the kernel must create it itself (device::init_in_first_process) instead of
# panicking ("path resolution did not reach the final target").
#
#   boot disk  : /tmp/drm-m8-dev/boot.ext4  (kernel + /init + dtb, ext4)
#
# Usage: bash build_m8_devfix.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="/tmp/drm-m8-dev"
M7="/tmp/drm-m7"
CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"

mkdir -p "${OUT}"

# 1. cross-compile the /init (static glibc)
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${OUT}/nodev-init" "${SRC_DIR}/nodev_init.c"

# 2. minimal initramfs with **no /dev** (only /proc /sys /tmp so the init can
#    breathe). This is the exact condition that used to panic the kernel.
STAGE="$(mktemp -d)"
mkdir -p "${STAGE}/initroot/proc" "${STAGE}/initroot/sys" "${STAGE}/initroot/tmp"
cp "${OUT}/nodev-init" "${STAGE}/initroot/init"
( cd "${STAGE}/initroot" && find . | cpio -o -H newc 2>/dev/null > "${OUT}/initramfs.cpio" )
rm -rf "${STAGE}"

# 3. boot disk: kernel + initramfs + dtb (u-boot/dtb seeded from M7)
[[ -f "${KERNEL_IMAGE}" ]] || { echo "missing kernel image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -f "${OUT}/u-boot" ]] || cp "${M7}/u-boot" "${OUT}/u-boot"
[[ -f "${OUT}/qemu-virt.dtb" ]] || cp "${M7}/qemu-virt.dtb" "${OUT}/qemu-virt.dtb"

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUT}/initramfs.cpio" "${STAGE}/initramfs.cpio"
cp "${OUT}/qemu-virt.dtb" "${STAGE}/qemu-virt.dtb"
rm -f "${OUT}/boot.ext4"
truncate -s 128M "${OUT}/boot.ext4"
mkfs.ext4 -q -F -d "${STAGE}" "${OUT}/boot.ext4"
rm -rf "${STAGE}"

echo "built DRM-M8 dev-fix boot disk under ${OUT}"
