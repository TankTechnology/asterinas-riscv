#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Builds the static legacy-KMS probe and a one-file initramfs for it.
#
# The probe source is architecture-neutral; only the compiler changes. The
# initramfs contains a single `/init`, which mounts devtmpfs and runs the
# probe, so the kernel under test needs no other userspace.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: build_probe.sh --arch ARCH --output DIR [OPTIONS]

Options:
  --arch ARCH     amd64 or riscv64 (required).
  --output DIR    Directory receiving `init` and `probe-initramfs.cpio`.
  --source PATH   Probe source file (default: virtio_gpu_probe.c next to
                  this script; use prime_probe.c for PRIME).
  --cc PATH       C compiler (default: gcc for amd64,
                  riscv64-linux-gnu-gcc for riscv64).
  --keep-binary   Also copy the probe to DIR/virtio-gpu-probe.

Prints the initramfs path on success.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

ARCH=""
OUTPUT=""
CC=""
SOURCE=""
KEEP_BINARY=0

while (( $# > 0 )); do
    case "$1" in
        --arch | --output | --cc | --source)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --arch) ARCH="$2" ;;
                --output) OUTPUT="$2" ;;
                --cc) CC="$2" ;;
                --source) SOURCE="$2" ;;
            esac
            shift 2
            ;;
        --keep-binary) KEEP_BINARY=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ "$ARCH" == "amd64" || "$ARCH" == "riscv64" ]] ||
    die_usage "--arch must be 'amd64' or 'riscv64'"
[[ -n "$OUTPUT" ]] || die_usage "--output is required"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "$SOURCE" ]] || SOURCE="$SCRIPT_DIR/virtio_gpu_probe.c"
[[ -f "$SOURCE" ]] || {
    printf 'error: probe source is missing: %s\n' "$SOURCE" >&2
    exit 1
}

if [[ -z "$CC" ]]; then
    case "$ARCH" in
        riscv64) CC="${RISCV_CC:-riscv64-linux-gnu-gcc}" ;;
        amd64) CC="${CC_AMD64:-gcc}" ;;
    esac
fi
command -v "$CC" >/dev/null 2>&1 || {
    printf 'error: C compiler not found: %s\n' "$CC" >&2
    exit 1
}
command -v cpio >/dev/null 2>&1 || {
    printf 'error: cpio is required to pack the initramfs\n' >&2
    exit 1
}

mkdir -p -- "$OUTPUT"
ROOTFS="$OUTPUT/rootfs"
rm -rf -- "$ROOTFS"
mkdir -p -- "$ROOTFS"

"$CC" -static -O2 -Wall -Wextra -o "$ROOTFS/init" "$SOURCE"
if [[ $KEEP_BINARY -eq 1 ]]; then
    cp -- "$ROOTFS/init" "$OUTPUT/virtio-gpu-probe"
fi

INITRAMFS="$OUTPUT/probe-initramfs.cpio"
( cd "$ROOTFS" && find . -print0 | cpio --null -o -H newc 2>/dev/null ) >"$INITRAMFS"

printf '%s\n' "$INITRAMFS"
