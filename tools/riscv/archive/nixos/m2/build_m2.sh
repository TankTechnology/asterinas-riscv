#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M2: assemble an initramfs with a dynamically linked musl riscv64 binary.
# Layout: /init (static glibc launcher), /bin/busybox (M1 artifact),
# /bin/hello_dyn (dynamic musl), /lib/{ld-musl-riscv64.so.1,libc.so,libgreet.so}.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
BUILD_ROOT="${NIXOS_ROOT}/m2"
BUSYBOX="${NIXOS_ROOT}/busybox"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/m2-initramfs.cpio.gz}"

CC_MUSL="/usr/bin/riscv64-linux-musl-gcc"
CC_STATIC="riscv64-linux-gnu-gcc"
MUSL_LIB="/usr/riscv64-linux-musl/lib"

if [[ ! -x "${CC_MUSL}" ]]; then
    echo "missing ${CC_MUSL}; install the musl-riscv64 package" >&2
    exit 2
fi
if [[ ! -x "${BUSYBOX}" ]]; then
    echo "missing ${BUSYBOX}; run ../build_busybox.sh first" >&2
    exit 2
fi

mkdir -p "${BUILD_ROOT}"

# 1. Shared library (tests DT_NEEDED resolution).
"${CC_MUSL}" -fPIC -shared -O2 \
    -o "${BUILD_ROOT}/libgreet.so" "${SRC_DIR}/libgreet.c"

# 2. Dynamic main binary linking musl libc + libgreet.
"${CC_MUSL}" -O2 \
    -L"${BUILD_ROOT}" -lgreet -Wl,-rpath,/lib \
    -o "${BUILD_ROOT}/hello_dyn" "${SRC_DIR}/hello.c"

# 3. Static /init launcher (glibc static, same pattern as M1).
"${CC_STATIC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${BUILD_ROOT}/init" "${SRC_DIR}/init_m2.c"

# 4. Assemble rootfs.
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/bin" "${ROOTFS}/lib" "${ROOTFS}/dev" \
    "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

cp "${BUILD_ROOT}/init" "${ROOTFS}/init"
cp "${BUILD_ROOT}/hello_dyn" "${ROOTFS}/bin/hello_dyn"
cp "${BUILD_ROOT}/libgreet.so" "${ROOTFS}/lib/libgreet.so"
# musl convention: the dynamic loader IS libc. One file serves both roles;
# the ELF interpreter of the binaries points at /lib/ld-musl-riscv64.so.1.
MUSL_LIBC="${MUSL_LIB}/musl/lib/libc.so"
cp "${MUSL_LIBC}" "${ROOTFS}/lib/ld-musl-riscv64.so.1"
echo "musl loader+libc: ld-musl-riscv64.so.1 (single shared object)"

# Busybox + a few applet symlinks for the final shell marker.
cp "${BUSYBOX}" "${ROOTFS}/bin/busybox"
for applet in sh ls echo; do
    ln -sf busybox "${ROOTFS}/bin/${applet}"
done

# 5. Pack as newc cpio + gzip.
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
echo "  hello_dyn: $(file -b "${BUILD_ROOT}/hello_dyn" | cut -c1-60)"
