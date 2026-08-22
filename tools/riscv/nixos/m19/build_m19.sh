#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Pack the DRM-M19 Debian riscv64 rootfs (Mesa 25.0.7 with virgl) into an
# initramfs and a boot disk for boot_m19_virgl.py.
#
# Usage:
#     bash build_m19.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
DISK_DIR="${REPO_ROOT}/target/drm-m19"
ROOTFS="${REPO_ROOT}/target/m19/rootfs"

OUTPUT="${DISK_DIR}/initramfs.cpio.gz"
BOOT_DISK="${DISK_DIR}/boot.ext4"
U_BOOT="${DISK_DIR}/u-boot"
DTB="${DISK_DIR}/qemu-virt.dtb"

KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
SRC_DISK="${REPO_ROOT}/target/drm-m16"

mkdir -p "${DISK_DIR}"
if [[ ! -f "${U_BOOT}" ]]; then cp "${SRC_DISK}/u-boot" "${U_BOOT}"; fi
if [[ ! -f "${DTB}" ]]; then cp "${SRC_DISK}/qemu-virt-smp4.dtb" "${DTB}"; fi

# Install the init script and build the in-guest test binaries.
cp "$(dirname "${BASH_SOURCE[0]}")/init.sh" "${ROOTFS}/init"
chmod +x "${ROOTFS}/init"

GNUCC="${RISC_V_GNU_CC:-riscv64-linux-gnu-gcc}"
MUSLCC="${RISC_V_MUSL_CC:-riscv64-linux-musl-gcc}"
MESAINC="${MESA_HEADERS:-/tmp/m16-dev/usr/include}"

mkdir -p "${ROOTFS}/root"
"${GNUCC}" -O2 -o "${ROOTFS}/root/eglrender2" \
    "$(dirname "${BASH_SOURCE[0]}")/eglrender2.c" \
    -I"${MESAINC}" -L"${ROOTFS}/usr/lib/riscv64-linux-gnu" \
    -Wl,-rpath-link,"${ROOTFS}/usr/lib/riscv64-linux-gnu" \
    "${ROOTFS}/usr/lib/riscv64-linux-gnu/libEGL.so.1" \
    "${ROOTFS}/usr/lib/riscv64-linux-gnu/libGLESv2.so.2" \
    "${ROOTFS}/usr/lib/riscv64-linux-gnu/libgbm.so.1"
"${MUSLCC}" -O2 -static -o "${ROOTFS}/root/virgltest" \
    "${REPO_ROOT}/tools/riscv/nixos/m16/virgltest.c"

echo "==> packing initramfs (Debian rootfs, this takes a moment)"
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )
echo "built ${OUTPUT} ($(stat -c%s "${OUTPUT}") bytes)"

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
rm -f "${BOOT_DISK}"
INITRD_BYTES=$(stat -c%s "${OUTPUT}")
KERNEL_BYTES=$(stat -c%s "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 32*1024*1024) / 1024 / 1024 + 1 ))
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "packed ${BOOT_DISK} (${BOOT_MB}M)"
