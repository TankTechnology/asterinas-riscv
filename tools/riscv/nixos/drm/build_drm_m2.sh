#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the DRM-M2 initramfs: a single static /init that drives the DRM node
# through the standard KMS ioctl path, draws two frames, and reports markers the
# host screendump harness verifies.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/drm-m2"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/drm-initramfs.cpio.gz}"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_m2.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
