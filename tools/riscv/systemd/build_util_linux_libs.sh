#!/usr/bin/env bash
# Cross-compile the util-linux libraries systemd depends on, for riscv64 glibc:
#   libuuid libblkid libmount (and, opportunistically, libfdisk libsmartcols).
# Installs static .a + headers + .pc into target/riscv-cross/usr (prefix).
#
# We build ONLY the libraries (--disable-all-programs) — the mount/umount/…
# binaries are not needed for the systemd *build* (systemd links against the
# libs; the runtime tools come later / from the NixOS closure).
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
VERSION=2.40.4
JOBS="$(nproc)"

export CC="$HOST-gcc"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"

mkdir -p "$SRC"
cd "$SRC"

tarball="util-linux-$VERSION.tar.xz"
dir="util-linux-$VERSION"
BASE=https://cdn.kernel.org/pub/linux/utils/util-linux/v2.40

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
  # --disable-all-programs sets the default enable-state to "no" for every
  # library AND program; re-enable just the libraries systemd links against.
  ./configure \
    --host="$HOST" --prefix="$PREFIX" \
    --disable-shared --enable-static \
    --disable-all-programs \
    --enable-libuuid --enable-libblkid --enable-libmount \
    --enable-libsmartcols --enable-libfdisk \
    --without-python --without-systemd --without-udev \
    --without-btrfs --without-ncursesw --without-readline \
    --without-cap-ng --without-libmagic --without-utempter \
    --without-selinux --without-audit --without-cryptsetup \
    --without-smack --without-econf --without-tinfo \
    --with-libz
fi

make -j"$JOBS"
make install

echo "=== verify ==="
rc=0
for name in uuid blkid mount fdisk smartcols; do
  if [ -f "$PREFIX/lib/lib$name.a" ]; then
    echo "OK   lib$name.a"
  else
    echo "MISS lib$name.a"
    rc=1
  fi
done
exit "$rc"
