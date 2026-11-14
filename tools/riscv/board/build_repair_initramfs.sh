#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Build the repair initramfs the U-Boot fallback path boots.
#
#   tools/riscv/board/build_repair_initramfs.sh [output] [ssh-target]
#
# The image carries a static busybox and the board's own e2fsck with the
# libraries it links against, fetched read-only from a running board. Its init
# runs `e2fsck -f -y` on the root and data partitions while they are unmounted
# and then hands over a shell, which is what the STALE-file-handle dirents and
# the EUCLEAN flag on the data partition need.
#
# Fetching the board's e2fsck instead of building e2fsprogs for riscv64 keeps
# the image and the filesystem tools the same version, and needs no cross
# toolchain beyond the statically linked busybox.
set -euo pipefail

OUT=${1:-target/board/repair.cpio.gz}
TARGET=${2:-root@58.206.232.122}
PORT=${BOARD_PORT:-2222}
KEY=${BOARD_KEY:-$HOME/.ssh/asterinas_megrez}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BUSYBOX=${BUSYBOX:-/tmp/qprobe/root/bin/busybox}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

LIBS="libext2fs.so.2 libcom_err.so.2 libblkid.so.1 libuuid.so.1 libe2p.so.2 libc.so.6"

mkdir -p "$WORK"/{bin,sbin,lib/riscv64-linux-gnu,proc,sys,dev,tmp}

[ -x "$BUSYBOX" ] || { echo "no static busybox at $BUSYBOX (set BUSYBOX=)" >&2; exit 1; }
install -m 0755 "$BUSYBOX" "$WORK/bin/busybox"
# The kernel execs /init through its shebang, so /bin/sh must exist before
# anything in the image runs. Without this the init dies with ENOENT and the
# kernel panics before printing a single line of the repair script.
ln -sf busybox "$WORK/bin/sh"
for applet in mount umount cat echo ls; do ln -sf busybox "$WORK/bin/$applet"; done

# One tar round trip: e2fsck plus its libraries, following symlinks.
{
    echo "sbin/e2fsck"
    for lib in $LIBS; do echo "lib/riscv64-linux-gnu/$lib"; done
    echo "lib/ld-linux-riscv64-lp64d.so.1"
} >"$WORK/manifest.txt"
ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o BatchMode=yes "$TARGET" \
    "tar -C / -czhf - $(tr '\n' ' ' <"$WORK/manifest.txt")" |
    tar -xzf - -C "$WORK"
chmod 0755 "$WORK/sbin/e2fsck"

install -m 0755 "$HERE/repair/init" "$WORK/init"

mkdir -p "$(dirname "$OUT")"
(cd "$WORK" && find . | cpio -o -H newc --quiet | gzip -9) >"$OUT"
echo "repair initramfs: $OUT ($(stat -c %s "$OUT") bytes)"
