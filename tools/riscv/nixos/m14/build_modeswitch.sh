#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DRM-M14: cross-compile the KMS mode-switch smoke test and assemble a minimal
# boot disk. The initramfs holds a single static /init (modeswitch) that drives
# DRM_IOCTL_MODE_SETCRTC between two resolutions on /dev/dri/card0 — no systemd,
# no Xorg. The boot disk is the same layout as the M10 desktop disk (kernel +
# initramfs + a correct 1-CPU/2G DTB) so it boots through the same u-boot path.
#
#   /tmp/m14-modeswitch/boot.ext4
#
# Usage: bash build_modeswitch.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DESKTOP_TREE="${DESKTOP_TREE:-$HOME/Program/asterinas-riscv}"
DISK_DIR="/tmp/m14-modeswitch"
ROOTFS="${REPO_ROOT}/target/nixos/m14/rootfs"
INITRAMFS="${REPO_ROOT}/target/nixos/m14/initramfs.cpio"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
U_BOOT_SRC="${DESKTOP_TREE}/target/qemu-uboot/cache/u-boot-build/u-boot"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
STRIP="riscv64-linux-gnu-strip"

[[ -f "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -f "${U_BOOT_SRC}" ]] || { echo "missing u-boot: ${U_BOOT_SRC}" >&2; exit 2; }

# --- 1. cross-compile the modeswitch test -----------------------------------
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/modeswitch.c"
"${STRIP}" --strip-unneeded "${ROOTFS}/init"
echo "built modeswitch /init ($(stat -c%s "${ROOTFS}/init") bytes)"

# --- 2. pack a raw newc initramfs -------------------------------------------
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${INITRAMFS}" )
echo "built ${INITRAMFS} ($(stat -c%s "${INITRAMFS}") bytes)"

# --- 3. repack the boot disk (kernel + initramfs + correct 1-CPU/2G DTB) -----
mkdir -p "${DISK_DIR}"
if [[ ! -f "${DISK_DIR}/u-boot" ]]; then
    cp "${U_BOOT_SRC}" "${DISK_DIR}/u-boot"
fi

DTB="${DISK_DIR}/qemu-virt.dtb"
qemu-system-riscv64 -machine virt -m 2G -smp 1 \
    -cpu 'rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true' \
    -machine "dumpdtb=${DTB}" -nographic >/dev/null 2>&1 || true
echo "regenerated ${DTB}"

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${INITRAMFS}" "${STAGE}/initramfs.cpio"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
BOOT_DISK="${DISK_DIR}/boot.ext4"
rm -f "${BOOT_DISK}"
INITRD_BYTES=$(stat -c%s "${INITRAMFS}")
KERNEL_BYTES=$(stat -c%s "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 32*1024*1024) / 1024 / 1024 + 1 ))
FLOOR_MB=256
if (( BOOT_MB < FLOOR_MB )); then BOOT_MB=${FLOOR_MB}; fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "re-packed ${BOOT_DISK} (${BOOT_MB}M)"
