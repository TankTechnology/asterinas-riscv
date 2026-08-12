#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build the minimal Wayland protocol-verification demo for the RISC-V QEMU
# framebuffer chain, and pack it as the marker initramfs.
#
# The demo is a single static riscv64 /init: the main process is a tiny Wayland
# compositor that maps /dev/fb0, listens on an AF_UNIX socket, and forks a
# client child that submits a memfd-backed wl_shm buffer. On surface commit the
# compositor blits the client's buffer to the screen, proving the Wayland
# protocol path (socket + SCM_RIGHTS + shm) end to end.
#
# Usage: build_wayland.sh [output.cpio.gz]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${1:-${REPO_ROOT}/target/qemu-uboot/initramfs-wayland.cpio.gz}"
BUILD_DIR="${REPO_ROOT}/target/wayland"

CC="riscv64-linux-gnu-gcc"
CFLAGS="-O2 -static -no-pie -fno-stack-protector"

mkdir -p "${BUILD_DIR}"
"${CC}" ${CFLAGS} -o "${BUILD_DIR}/init" \
    "${SRC_DIR}/wire.c" \
    "${SRC_DIR}/compositor.c" \
    "${SRC_DIR}/client.c"

python3 "${REPO_ROOT}/tools/riscv/make_qemu_uboot_initramfs.py" \
    --init-elf "${BUILD_DIR}/init" "${OUTPUT}"

echo "built ${OUTPUT}"
