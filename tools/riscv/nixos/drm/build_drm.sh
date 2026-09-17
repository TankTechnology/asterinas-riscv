#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the DRM-M1 initramfs: a single static /init that opens the
# virtio-gpu DRM node and queries the driver version.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/drm"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/drm-initramfs.cpio.gz}"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
