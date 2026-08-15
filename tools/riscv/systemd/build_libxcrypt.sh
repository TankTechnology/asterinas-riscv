#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Cross-compile libxcrypt for riscv64 glibc into target/riscv-cross/usr.
# systemd links crypt() unconditionally; glibc 2.41 (Debian) removed libcrypt
# into libxcrypt, and the cross sysroot ships neither — so systemd's
# dependency('libcrypt','libxcrypt') + find_library('crypt') fallback both fail.
# libxcrypt is a single autotools lib with no deps: the easiest of the "easy
# deps first" set.
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
VERSION=4.4.38
JOBS="$(nproc)"

export CC="$HOST-gcc"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"

mkdir -p "$SRC"
cd "$SRC"

# NOTE: the github "releases/download" asset stalls on this network (SSL EOF,
# 2min timeout). The Debian pool hosts the same upstream orig tarball reliably.
tarball="libxcrypt-${VERSION}.orig.tar.xz"
dir="libxcrypt-$VERSION"
BASE=https://deb.debian.org/debian/pool/main/libx/libxcrypt

if [ ! -d "$dir" ]; then
  if [ ! -f "$tarball" ]; then
    echo "  downloading $tarball"
    curl -fsSL --retry 6 --retry-all-errors --retry-delay 2 -o "$tarball" "$BASE/${tarball}"
  fi
  echo "  extracting"
  tar xf "$tarball"
fi

cd "$dir"
# The upstream orig tarball ships configure.ac but NOT a generated configure
# (it is a git archive). Regenerate it; the required autoconf-archive macros
# are bundled under build-aux/m4 (see AC_CONFIG_MACRO_DIR).
if [ ! -f configure ]; then
  autoreconf -fiv -Wall
fi
if [ ! -f Makefile ]; then
  ./configure --host="$HOST" --prefix="$PREFIX" --disable-shared --enable-static
fi
make -j"$JOBS"
make install

echo "=== verify ==="
rc=0
for f in libcrypt.a; do
  if [ -f "$PREFIX/lib/$f" ]; then echo "OK   $f"; else echo "MISS $f"; rc=1; fi
done
if [ -f "$PREFIX/include/crypt.h" ]; then echo "OK   crypt.h"; else echo "MISS crypt.h"; rc=1; fi
if [ -f "$PREFIX/lib/pkgconfig/libcrypt.pc" ] || [ -f "$PREFIX/lib/pkgconfig/libxcrypt.pc" ]; then
  echo "OK   libcrypt.pc"
else
  echo "MISS libcrypt.pc (will check pkg-config resolve below)"; rc=1
fi
exit "$rc"
