#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the FOUNDATION-M4 initramfs: a single static /init smoke binary that
# exercises fanotify, the keyring syscalls and seccomp strict mode, packed as
# newc cpio + gzip.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/fm4"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/fm4-initramfs.cpio.gz}"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
