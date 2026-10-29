#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Build the Stage1 initramfs used by the upstream-rebase Debian line.
#
# The full downstream Stage1 builder also packages the browser/desktop helpers.
# This first migration version keeps only the root handoff and accepts an
# optional tools directory so that a browser-web rootfs can still receive the
# ephemeral /usr/lib/asterinas helpers it expects.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: build_stage1_minimal.sh [OUTPUT]
       STAGE1_TOOLS_DIR=/path/to/tools build_stage1_minimal.sh [OUTPUT]

Environment:
  RISC_V_CC         RISC-V Linux compiler (default: riscv64-linux-gnu-gcc)
  STAGE1_TOOLS_DIR  Optional directory copied into the initramfs root
                    (for example, usr/lib/asterinas from an existing image)
EOF
}

if (( $# > 1 )); then
    usage >&2
    exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../../../.." && pwd)"
OUTPUT="${1:-$REPOSITORY_ROOT/target/riscv-debian/stage1/initramfs.cpio}"
COMPILER="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
TOOLS_DIR="${STAGE1_TOOLS_DIR:-}"

for command in "$COMPILER" cpio; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "error: required tool not found: $command" >&2
        exit 1
    fi
done

if [[ -n "$TOOLS_DIR" ]] && [[ ! -d "$TOOLS_DIR" ]]; then
    echo "error: STAGE1_TOOLS_DIR is not a directory: $TOOLS_DIR" >&2
    exit 2
fi
if [[ -n "$TOOLS_DIR" ]] && [[ -e "$TOOLS_DIR/init" ]]; then
    echo "error: STAGE1_TOOLS_DIR must not contain init" >&2
    exit 2
fi

case "$OUTPUT" in
    '' | */ | *$'\n'* | *$'\r'* | *$'\t'*)
        echo "error: unsafe output path: $OUTPUT" >&2
        exit 2
        ;;
esac

OUTPUT_DIR="$(dirname -- "$OUTPUT")"
mkdir -p -- "$OUTPUT_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf -- "$STAGE"' EXIT

"$COMPILER" -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror \
    "$SCRIPT_DIR/stage1_init.c" \
    "$SCRIPT_DIR/stage1_debug_console.c" \
    "$SCRIPT_DIR/stage1_probe.c" \
    -o "$STAGE/init"

if [[ -n "$TOOLS_DIR" ]]; then
    cp -a -- "$TOOLS_DIR"/. "$STAGE"/
fi

(
    cd "$STAGE"
    find . -mindepth 1 -printf '%P\n' | LC_ALL=C sort |
        cpio --quiet --reproducible --owner=0:0 --create --format=newc
) > "$OUTPUT"

echo "Stage1 initramfs: $OUTPUT"
