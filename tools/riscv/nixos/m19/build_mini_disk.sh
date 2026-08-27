#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Builds the Mesa mini-root as a second ext2 disk plus a small bootstrap
# initramfs. This avoids unpacking the large Mesa/LLVM closure in TCG.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
BUILD_DIR="${REPO_ROOT}/target/drm-m19/mini-disk"
ROOTFS="${BUILD_DIR}/rootfs"
BOOTSTRAP="${BUILD_DIR}/bootstrap"
ROOT_DISK="/tmp/mini-virgl2.ext2"
INITRAMFS="/tmp/mini-virgl2.cpio.gz"

mkdir -p "${BUILD_DIR}" "${BOOTSTRAP}/bin" "${BOOTSTRAP}/dev"
python3 "${REPO_ROOT}/tools/riscv/nixos/m19/build_mini_rootfs.py" "${ROOTFS}"

truncate -s 384M "${ROOT_DISK}"
# Asterinas's ext2 implementation currently requires 4096-byte blocks.
mkfs.ext2 -q -F -b 4096 -d "${ROOTFS}" "${ROOT_DISK}"

cp "${ROOTFS}/bin/busybox" "${BOOTSTRAP}/bin/busybox"
cp "${REPO_ROOT}/tools/riscv/nixos/m19/init_mini_disk.sh" "${BOOTSTRAP}/init"
chmod +x "${BOOTSTRAP}/init"
mknod -m 600 "${BOOTSTRAP}/dev/vdb" b 254 16 2>/dev/null || true

( cd "${BOOTSTRAP}" && find . -print0 | \
    cpio --null -o -H newc 2>/dev/null | gzip -1 > "${INITRAMFS}" )

echo "built ${ROOT_DISK} and ${INITRAMFS}"
