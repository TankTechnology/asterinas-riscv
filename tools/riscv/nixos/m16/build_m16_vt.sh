#!/bin/bash
# DRM-M16 build script: cross-compile the VT verification init + build initramfs.
# Produces /tmp/drm-m16/initramfs.cpio.gz for use with boot_m16_vt.py.
set -euo pipefail

OUTDIR="${OUTDIR:-/tmp/drm-m16}"
mkdir -p "$OUTDIR"

SRC="$(dirname "$0")/init.c"
TARGET="${OUTDIR}/init"

echo "==> Building M16 VT verification init"
riscv64-linux-gnu-gcc -static -o "$TARGET" "$SRC"
echo "    $TARGET ($(wc -c < "$TARGET") bytes)"

echo "==> Building initramfs"
INITRAMFS="${OUTDIR}/initramfs.cpio.gz"
STAGING="${OUTDIR}/initramfs_staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp "$TARGET" "${STAGING}/init"
chmod +x "${STAGING}/init"
( cd "$STAGING" && find . | cpio -o --format=newc 2>/dev/null | gzip > "$INITRAMFS" )
rm -rf "$STAGING"
echo "    $INITRAMFS ($(wc -c < "$INITRAMFS") bytes)"
echo "==> Done"