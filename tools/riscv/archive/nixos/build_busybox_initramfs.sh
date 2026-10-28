#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the M1 BusyBox initramfs: a static /init launcher, the static
# riscv64 BusyBox, and /bin symlinks for the smoke-test applets, packed as
# newc cpio + gzip.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos"
BUSYBOX="${BUILD_ROOT}/busybox"
ROOTFS="${BUILD_ROOT}/busybox-rootfs"
OUTPUT="${1:-${BUILD_ROOT}/busybox-initramfs.cpio.gz}"

CC="riscv64-linux-gnu-gcc"

# Applets exposed as /bin symlinks (busybox multi-call dispatch).
readonly -a APPLETS=(
    sh ls cat mount ps
    umount mountpoint mkdir rm rmdir ln mknod chmod chown
    echo printf test grep find head tail dd df free uname sync stat sleep kill pidof
    true false yes
)

if [[ ! -x "${BUSYBOX}" ]]; then
    echo "missing ${BUSYBOX}; run build_busybox.sh first" >&2
    exit 2
fi

rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/bin" "${ROOTFS}/dev" "${ROOTFS}/proc" \
    "${ROOTFS}/sys" "${ROOTFS}/tmp"

# /init launcher (static).
"${CC}" -O2 -static -no-pie -fno-stack-protector -o "${ROOTFS}/init" "${SRC_DIR}/init.c"

# BusyBox and applet symlinks.
cp "${BUSYBOX}" "${ROOTFS}/bin/busybox"
for applet in "${APPLETS[@]}"; do
    ln -sf busybox "${ROOTFS}/bin/${applet}"
done

# Pack as newc cpio + gzip.
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
