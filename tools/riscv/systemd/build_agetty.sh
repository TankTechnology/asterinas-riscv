#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Cross-compile util-linux's agetty (login terminal) for riscv64 glibc.
#
# agetty is the one "login component" that ships with util-linux (the same tree
# that already provides libmount/libblkid/libuuid). It is a small standalone
# termios program (links only util-linux's internal libcommon) that execs
# /bin/login after the baud/term negotiation — no PAM, no NSS, no glib stack, so
# it static-links cleanly and is the natural getty for a minimal systemd rootfs.
#
# NOTE: we reconfigure the EXISTING util-linux tree (adds --enable-agetty on top
# of the M2 library flags) and build ONLY the `agetty` target, then copy the
# binary into the prefix by hand. We deliberately do NOT re-run `make install`:
# that would overwrite libmount.a/libblkid.a/libsmartcols.a with fresh archives
# that still carry the GLOBAL parse_size/parse_range symbols, undoing the M2
# objcopy-localize fix. The library archives in the prefix are left untouched.
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
JOBS="$(nproc)"

export CC="$HOST-gcc"

U="$SRC/util-linux-2.40.4"
[ -d "$U" ] || { echo "missing $U (run build_util_linux_libs.sh first)"; exit 1; }
cd "$U"

# Reconfigure the same tree with agetty enabled (idempotent; keeps the M2
# library flags intact, only flips agetty's ENABLE_AGETTY).
./configure \
  --host="$HOST" --prefix="$PREFIX" \
  --disable-shared --enable-static \
  --disable-all-programs \
  --enable-libuuid --enable-libblkid --enable-libmount \
  --enable-libsmartcols --enable-libfdisk \
  --enable-agetty \
  --disable-nls \
  --without-python --without-systemd --without-udev \
  --without-btrfs --without-ncursesw --without-readline \
  --without-cap-ng --without-libmagic --without-utempter \
  --without-selinux --without-audit --without-cryptsetup \
  --without-smack --without-econf --without-tinfo \
  --with-libz

# Build agetty twice (does not touch the installed .a archives):
#   1. dynamic-libc (the default link, PIE against libc.so.6 only)
#   2. fully static  (-all-static: no interpreter, links libc.a)
# The static one is the primary deliverable — a minimal systemd rootfs has no
# dynamic linker yet, so only the static getty can actually exec. The static
# link emits the usual NSS warnings (getgrnam/getaddrinfo -> dlopen at runtime),
# the same caveat the project has already mapped; agetty degrades gracefully
# when those lookups fail.
make -j"$JOBS" agetty
cp -f agetty "$U/agetty.dyn"
rm -f agetty
make -j"$JOBS" agetty LDFLAGS="-all-static"

# Install by hand: strip + copy the STATIC binary into the prefix sbin.
mkdir -p "$PREFIX/sbin"
"$HOST-strip" -o "$PREFIX/sbin/agetty" agetty
"$HOST-strip" -o "$PREFIX/sbin/agetty.dyn" "$U/agetty.dyn"

echo "=== verify ==="
file "$PREFIX/sbin/agetty"
readelf -d "$PREFIX/sbin/agetty" 2>/dev/null | grep -E 'NEEDED|interpreter' \
  || echo "  (no dynamic section -> fully static)"
exit 0
