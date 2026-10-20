#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

if (( $# > 1 )); then
    printf 'usage: build_virgl_param_gate.sh [OUTPUT]\n' >&2
    exit 2
fi
if [[ "${1:-}" == "--print-tools" ]]; then
    printf 'riscv64-linux-gnu-gcc\npython3\n'
    exit 0
fi
if [[ "${1:-}" == -* ]]; then
    printf 'usage: build_virgl_param_gate.sh [OUTPUT]\n' >&2
    exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "$TOOLS_DIR/../.." && pwd)"
SOURCE="$SCRIPT_DIR/virgl_param_gate_init.c"
OUTPUT="${1:-$REPOSITORY_ROOT/target/drm-virgl/initramfs.cpio.gz}"
COMPILER="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

command -v "$COMPILER" >/dev/null 2>&1 || {
    printf 'error: required compiler not found: %s\n' "$COMPILER" >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || {
    printf 'error: required tool not found: python3\n' >&2
    exit 1
}

BUILD_DIRECTORY="$(mktemp -d)"
cleanup() { rm -rf -- "$BUILD_DIRECTORY"; }
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"$COMPILER" -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror \
    -march=rv64gc -mabi=lp64d "$SOURCE" -o "$BUILD_DIRECTORY/init"
PYTHONPATH="$TOOLS_DIR" python3 "$TOOLS_DIR/make_qemu_uboot_initramfs.py" \
    "$OUTPUT" --init-elf "$BUILD_DIRECTORY/init"
