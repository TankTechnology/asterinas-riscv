#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build the Snake demo for the RISC-V QEMU framebuffer chain and pack it as
# the marker initramfs. Only needs glibc (no third-party libraries).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${1:-${REPO_ROOT}/target/qemu-uboot/initramfs-snake.cpio.gz}"
BUILD_DIR="${REPO_ROOT}/target/snake"

CC="riscv64-linux-gnu-gcc"
CFLAGS="-O2 -static -no-pie -fno-stack-protector"

mkdir -p "${BUILD_DIR}"
"${CC}" ${CFLAGS} -o "${BUILD_DIR}/init" "${SRC_DIR}/snake.c"

python3 "${REPO_ROOT}/tools/riscv/make_qemu_uboot_initramfs.py" \
    --init-elf "${BUILD_DIR}/init" "${OUTPUT}"

echo "built ${OUTPUT}"
