#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build the libwayland client variant of the Wayland protocol demo and pack it
# as the marker initramfs.
#
# Unlike build_wayland.sh (hand-written wire codec), this links the real
# libwayland-client against the demo compositor. The libwayland dependency chain
# (libffi, expat, libwayland) must already be cross-compiled into
# target/riscv-cross/usr — see README.md for the cross-compile steps.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CROSS_USR="${REPO_ROOT}/target/riscv-cross/usr"
OUTPUT="${1:-${REPO_ROOT}/target/qemu-uboot/initramfs-wayland.cpio.gz}"
BUILD_DIR="${REPO_ROOT}/target/wayland"

CC="riscv64-linux-gnu-gcc"
CFLAGS="-O2 -static -no-pie -fno-stack-protector -I${CROSS_USR}/include"

# Cross-compile the dependency chain on first use.
if [[ ! -f "${CROSS_USR}/lib/libwayland-client.a" ]]; then
    bash "${SRC_DIR}/build_wayland_deps.sh"
fi

mkdir -p "${BUILD_DIR}"
"${CC}" ${CFLAGS} -o "${BUILD_DIR}/init" \
    "${SRC_DIR}/wire.c" \
    "${SRC_DIR}/compositor.c" \
    "${SRC_DIR}/client_libwayland.c" \
    -L"${CROSS_USR}/lib" -lwayland-client -lffi

python3 "${REPO_ROOT}/tools/riscv/make_qemu_uboot_initramfs.py" \
    --init-elf "${BUILD_DIR}/init" "${OUTPUT}"

echo "built ${OUTPUT}"
