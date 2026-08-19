#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Cross-compile the libwayland dependency chain for riscv64 into
# target/riscv-cross/usr, for the libwayland-client variant of the Wayland demo.
#
# The chain is:
#   libffi (autotools) -> expat (autotools) -> libwayland (meson)
#
# libwayland's version must match the host wayland-scanner version, because
# meson looks up a native wayland-scanner of the same version when
# cross-compiling. Requirements: riscv64-linux-gnu-gcc, autoconf/automake/
# libtool, meson, ninja, pkg-config, and network access for the sources.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CROSS_USR="${REPO_ROOT}/target/riscv-cross/usr"
SRC_DIR="${REPO_ROOT}/target/riscv-cross/src"

LIBFFI_VERSION="v3.5.2"
EXPAT_VERSION="R_2_7_1"
WAYLAND_VERSION="1.26.0"

CC="riscv64-linux-gnu-gcc"
HOST="riscv64-linux-gnu"

mkdir -p "${CROSS_USR}" "${SRC_DIR}"

build_libffi() {
    if [[ -f "${CROSS_USR}/lib/libffi.a" ]]; then
        echo "libffi already built"
        return
    fi
    cd "${SRC_DIR}"
    [[ -d libffi ]] || git clone --depth 1 --branch "${LIBFFI_VERSION}" https://github.com/libffi/libffi.git
    cd libffi
    [[ -f configure ]] || ./autogen.sh
    ./configure --host="${HOST}" --prefix="${CROSS_USR}" --disable-shared --enable-static
    make -j"$(nproc)"
    make install
}

build_expat() {
    if [[ -f "${CROSS_USR}/lib/libexpat.a" ]]; then
        echo "expat already built"
        return
    fi
    cd "${SRC_DIR}"
    [[ -d libexpat ]] || git clone --depth 1 --branch "${EXPAT_VERSION}" https://github.com/libexpat/libexpat.git
    cd libexpat/expat
    [[ -f configure ]] || ./buildconf.sh
    ./configure --host="${HOST}" --prefix="${CROSS_USR}" --disable-shared --enable-static \
        --without-examples --without-tests --without-docbook
    make -j"$(nproc)"
    make install
}

build_libwayland() {
    if [[ -f "${CROSS_USR}/lib/libwayland-client.a" ]]; then
        echo "libwayland already built"
        return
    fi
    cd "${SRC_DIR}"
    [[ -d wayland ]] || git clone --depth 1 --branch "${WAYLAND_VERSION}" https://gitlab.freedesktop.org/wayland/wayland.git
    cd wayland

    cat > "${SRC_DIR}/cross-riscv64.txt" <<EOF
[binaries]
c = '${HOST}-gcc'
cpp = '${HOST}-g++'
ar = '${HOST}-ar'
strip = '${HOST}-strip'
pkgconfig = 'pkg-config'

[host_machine]
system = 'linux'
cpu_family = 'riscv64'
cpu = 'riscv64'
endian = 'little'

[properties]
pkg_config_libdir = '${CROSS_USR}/lib/pkgconfig'
EOF

    rm -rf build
    meson setup build --cross-file "${SRC_DIR}/cross-riscv64.txt" \
        --prefix="${CROSS_USR}" --default-library=static \
        -Dtests=false -Ddocumentation=false -Ddtd_validation=false
    ninja -C build
    ninja -C build install
}

build_libffi
build_expat
build_libwayland

echo "libwayland dependency chain built into ${CROSS_USR}"
