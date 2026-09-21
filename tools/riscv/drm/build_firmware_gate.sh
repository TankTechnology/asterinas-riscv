#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: build_firmware_gate.sh [OUTPUT]
       build_firmware_gate.sh --print-tools

Build the static RISC-V firmware gate initramfs.

The probe pins the framebuffer layout it expects. The defaults are the
generic QEMU `BOCHS_XRGB8888` contract (1280x1024, stride 5120); set
GATE_MODE_WIDTH / GATE_MODE_HEIGHT / GATE_MODE_STRIDE to pin another. The
caller is expected to take those numbers from the device set that writes the
device tree node, so the expectation and the node cannot drift apart.
EOF
}

if (( $# > 1 )); then
    usage >&2
    exit 2
fi
if [[ "${1:-}" == "--print-tools" ]]; then
    printf 'riscv64-linux-gnu-gcc\npython3\n'
    exit 0
fi
if [[ "${1:-}" == -* ]]; then
    usage >&2
    exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "$TOOLS_DIR/../.." && pwd)"
SOURCE="$SCRIPT_DIR/firmware_gate_init.c"
OUTPUT="${1:-$REPOSITORY_ROOT/target/drm-firmware/initramfs.cpio.gz}"
COMPILER="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

if ! command -v "$COMPILER" >/dev/null 2>&1; then
    printf 'error: required compiler not found: %s\n' "$COMPILER" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf 'error: required tool not found: python3\n' >&2
    exit 1
fi

BUILD_DIRECTORY="$(mktemp -d)"
cleanup() {
    rm -rf -- "$BUILD_DIRECTORY"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mode_width="${GATE_MODE_WIDTH:-1280}"
mode_height="${GATE_MODE_HEIGHT:-1024}"
mode_stride="${GATE_MODE_STRIDE:-$((mode_width * 4))}"
for value in "$mode_width" "$mode_height" "$mode_stride"; do
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
        printf 'error: framebuffer geometry must be positive integers, got %s\n' \
            "$value" >&2
        exit 2
    fi
done
printf 'firmware gate geometry: %sx%s stride %s\n' \
    "$mode_width" "$mode_height" "$mode_stride"

"$COMPILER" -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror \
    -march=rv64gc -mabi=lp64d \
    -DMODE_WIDTH="${mode_width}U" \
    -DMODE_HEIGHT="${mode_height}U" \
    -DMODE_STRIDE="${mode_stride}U" \
    "$SOURCE" -o "$BUILD_DIRECTORY/init"
PYTHONPATH="$TOOLS_DIR" python3 "$TOOLS_DIR/make_qemu_uboot_initramfs.py" \
    "$OUTPUT" --init-elf "$BUILD_DIRECTORY/init"
