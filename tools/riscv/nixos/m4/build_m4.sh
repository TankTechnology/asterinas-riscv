#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M4: build the clone(CLONE_SETTLS) + SIGSEGV minimal repro as a static /init,
# assemble a tiny initramfs, and repack the QEMU boot disk with the current
# kernel Image + this initramfs. Mirrors the M2/M3 initramfs + boot-disk flow.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
BUILD_ROOT="${NIXOS_ROOT}/m4"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/m4-initramfs.cpio}"

KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"

CC_STATIC="riscv64-linux-gnu-gcc"

if ! command -v "${CC_STATIC}" >/dev/null 2>&1; then
    echo "missing ${CC_STATIC}; install riscv64-linux-gnu-gcc" >&2
    exit 2
fi

mkdir -p "${BUILD_ROOT}"

# 1. Static /init (glibc static, same pattern as M1/M2/M3). It runs the raw
#    clone CLONE_SETTLS + SIGSEGV checks, then execs /bin/tls_shared.
"${CC_STATIC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${BUILD_ROOT}/init" "${SRC_DIR}/tls_repro.c"

# 1b. Dynamic musl shared-TLS repro (general-dynamic TLS via the DTV). This is
#     the actual nix/Boole-GC blocker from M4. Needs the musl cross toolchain.
CC_MUSL="riscv64-linux-musl-gcc"
MUSL_LIBC="/usr/riscv64-linux-musl/lib/musl/lib/libc.so"
if ! command -v "${CC_MUSL}" >/dev/null 2>&1; then
    echo "warning: ${CC_MUSL} missing; skipping shared-TLS repro" >&2
else
    "${CC_MUSL}" -fPIC -shared -O2 \
        -o "${BUILD_ROOT}/libtls.so" "${SRC_DIR}/libtls.c"
    "${CC_MUSL}" -O2 -L"${BUILD_ROOT}" -ltls -Wl,-rpath,/lib \
        -o "${BUILD_ROOT}/tls_shared" "${SRC_DIR}/tls_shared.c"
fi

# 2. Assemble the rootfs. /dev must exist for the kernel's first-process stdio.
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp" \
    "${ROOTFS}/bin" "${ROOTFS}/lib"
cp "${BUILD_ROOT}/init" "${ROOTFS}/init"
if [[ -x "${BUILD_ROOT}/tls_shared" ]]; then
    cp "${BUILD_ROOT}/tls_shared" "${ROOTFS}/bin/tls_shared"
    cp "${BUILD_ROOT}/libtls.so" "${ROOTFS}/lib/libtls.so"
    # musl convention: the dynamic loader IS libc (one shared object).
    cp "${MUSL_LIBC}" "${ROOTFS}/lib/ld-musl-riscv64.so.1"
fi

# 3. Pack as newc cpio (uncompressed, like M3).
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )

# 4. Repack the boot disk with the current kernel Image + this initramfs.
STAGE="${REPO_ROOT}/target/qemu-uboot/current/.m4-stage"
rm -rf "${STAGE}"; mkdir -p "${STAGE}"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
rm -f "${BOOT_DISK}"
truncate -s 96M "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"

echo "built ${OUTPUT}"
echo "  init: $(file -b "${BUILD_ROOT}/init" | cut -c1-60)"
echo "  boot disk: ${BOOT_DISK}"
