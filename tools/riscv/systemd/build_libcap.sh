#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Cross-compile libcap (+ libpsx) for riscv64 glibc into target/riscv-cross/usr.
# systemd needs libcap for capability handling (pid1 + journald).
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
VERSION=2.75
JOBS="$(nproc)"

CC="$HOST-gcc"
AR="$HOST-ar"
RANLIB="$HOST-ranlib"

mkdir -p "$SRC"
cd "$SRC"

tarball="libcap-$VERSION.tar.gz"
dir="libcap-$VERSION"
BASE=https://cdn.kernel.org/pub/linux/libs/security/linux-privs/libcap2

if [ ! -d "$dir" ]; then
  if [ ! -f "$tarball" ]; then
    echo "  downloading $tarball"
    curl -fsSL -o "$tarball" "$BASE/$tarball"
  fi
  echo "  extracting"
  tar xf "$tarball"
fi

cd "$dir"

# libcap uses CC for target and BUILD_CC for the host-side _makenames helper.
# Build the static libraries only; skip the setcap post-step (RAISE_SETFCAP=no)
# and skip the shared/symlink step by installing via DESTDIR then copying the
# static artifacts, headers and .pc files we actually want.
make -C libcap \
  CC="$CC" BUILD_CC=gcc AR="$AR" RANLIB="$RANLIB" \
  lib=lib \
  -j"$JOBS" libcap.a libpsx.a

# The static archives land in the libcap/ subdir (not the tarball root).
# Copy the pieces systemd needs: libcap.a, libpsx.a, sys/capability.h and the
# pkg-config files. (The libcap Makefile also builds a host `_makenames` helper;
# we do not need it at runtime.)
mkdir -p "$PREFIX/lib" "$PREFIX/include/sys" "$PREFIX/lib/pkgconfig"
cp -f libcap/libcap.a "$PREFIX/lib/"
cp -f libcap/libpsx.a "$PREFIX/lib/"
cp -f libcap/include/sys/capability.h libcap/include/sys/psx_syscall.h \
      libcap/include/sys/securebits.h "$PREFIX/include/sys/"

for pc in libcap.pc libpsx.pc; do
  if [ -f "libcap/$pc" ]; then
    sed -e "s|^prefix=.*|prefix=$PREFIX|" \
        -e "s|^libdir=.*|libdir=\${prefix}/lib|" \
        -e "s|^includedir=.*|includedir=\${prefix}/include|" \
        "libcap/$pc" > "$PREFIX/lib/pkgconfig/$pc"
  fi
done

# Fallback: if no .pc was installed, write a minimal one.
if [ ! -f "$PREFIX/lib/pkgconfig/libcap.pc" ]; then
  cat > "$PREFIX/lib/pkgconfig/libcap.pc" <<EOF
prefix=$PREFIX
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: libcap
Description: POSIX 1003.1e capabilities (library)
Version: $VERSION
Libs: -L\${libdir} -lcap
Cflags: -I\${includedir}
EOF
fi

echo "=== verify ==="
rc=0
for f in libcap.a libpsx.a; do
  if [ -f "$PREFIX/lib/$f" ]; then echo "OK   $f"; else echo "MISS $f"; rc=1; fi
done
if [ -f "$PREFIX/include/sys/capability.h" ]; then echo "OK   sys/capability.h"; else echo "MISS sys/capability.h"; rc=1; fi
exit "$rc"
