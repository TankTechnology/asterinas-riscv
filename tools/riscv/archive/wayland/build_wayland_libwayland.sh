#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Build the real-libwayland variant of the Wayland protocol demo and pack it as
# the marker initramfs: a libwayland-server compositor forking a
# libwayland-client client. No hand-written wire codec is used.
#
# The libwayland dependency chain (libffi, expat, libwayland) is
# cross-compiled on first use by build_wayland_deps.sh into
# target/riscv-cross/usr.

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
    "${SRC_DIR}/compositor_libwayland.c" \
    "${SRC_DIR}/client_libwayland.c" \
    -L"${CROSS_USR}/lib" -lwayland-server -lwayland-client -lffi

python3 "${REPO_ROOT}/tools/riscv/make_qemu_uboot_initramfs.py" \
    --init-elf "${BUILD_DIR}/init" "${OUTPUT}"

echo "built ${OUTPUT}"
