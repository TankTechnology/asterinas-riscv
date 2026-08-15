#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Build + boot the minimal static repro (tools/riscv/nixos/ltp/repro.c) as a
# bare initramfs /init and collect its [PASS]/[FAIL] lines via boot_ltp_gate.py.
# This is the fastest way to probe one syscall's exact errno without a full LTP
# run.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "${REPO_ROOT}"

SRC_DIR="${REPO_ROOT}/tools/riscv/nixos/ltp"
CC="${RISC_V_CC:-riscv64-linux-musl-gcc}"
BUILD_ROOT="${REPO_ROOT}/target/ltp/repro"
OUTPUT="${BUILD_ROOT}/repro-initramfs.cpio.gz"
BOOT_DRIVER="${SRC_DIR}/boot_ltp_gate.py"
KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
SERIAL_LOG="${BUILD_ROOT}/repro-serial.log"

rm -rf "${BUILD_ROOT}"
mkdir -p "${BUILD_ROOT}/rootfs/dev" "${BUILD_ROOT}/rootfs/proc" \
         "${BUILD_ROOT}/rootfs/sys" "${BUILD_ROOT}/rootfs/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${BUILD_ROOT}/rootfs/init" "${SRC_DIR}/repro.c"

( cd "${BUILD_ROOT}/rootfs" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )
echo "built ${OUTPUT}"

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
truncate -s 128M "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"

python3 "${BOOT_DRIVER}" --smp 1 --serial-log "${SERIAL_LOG}" --command-timeout 120 --loglevel info
