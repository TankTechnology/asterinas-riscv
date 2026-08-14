#!/usr/bin/env bash
# Cross-compile the libX* extension libraries for the RISC-V desktop (static .a).
# Builds, in dependency order:
#   libXext libXrender libXfixes libXdamage libXcomposite
#   libXrandr libXi libXinerama libXcursor libXft
# Installs into target/riscv-cross/usr (prefix).
#
# Prereqs (already present in the cross tree): libX11, libxcb(+exts),
# freetype, fontconfig, and the xorgproto split .pc files in share/pkgconfig.
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
BASE=https://www.x.org/archive/individual/lib
JOBS="$(nproc)"

export CC="$HOST-gcc"
export CXX="$HOST-g++"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"

# "name version" pairs, in dependency order.
LIBS=(
  "libXext 1.3.7"
  "libXrender 0.9.12"
  "libXfixes 6.0.2"
  "libXdamage 1.1.7"
  "libXcomposite 0.4.7"
  "libXrandr 1.5.5"
  "libXi 1.8.3"
  "libXinerama 1.1.6"
  "libXcursor 1.2.3"
  "libXft 2.3.9"
)

mkdir -p "$SRC"
cd "$SRC"

for entry in "${LIBS[@]}"; do
  name="${entry%% *}"
  ver="${entry##* }"
  tarball="$name-$ver.tar.xz"
  dir="$name-$ver"

  echo "==== [$name $ver] ===="
  if [ ! -d "$dir" ]; then
    if [ ! -f "$tarball" ]; then
      echo "  downloading $tarball"
      curl -fsSL -o "$tarball" "$BASE/$tarball"
    fi
    echo "  extracting"
    tar xf "$tarball"
  fi

  cd "$dir"
  if [ ! -f Makefile ]; then
    ./configure --host="$HOST" --prefix="$PREFIX" --disable-shared --enable-static
  fi
  make -j"$JOBS"
  make install
  cd ..
  echo "==== done $name ===="
done

echo "=== verify ==="
rc=0
for name in libXext libXrender libXfixes libXdamage libXcomposite libXrandr libXi libXinerama libXcursor libXft; do
  if [ -f "$PREFIX/lib/$name.a" ]; then
    echo "OK   $name.a"
  else
    echo "MISS $name.a"
    rc=1
  fi
done
exit "$rc"
