#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: build_input_gate.sh [OUTPUT]
       build_input_gate.sh --print-tools
       build_input_gate.sh --print-entries

Build the RISC-V PCI xHCI input-gate initramfs.
EOF
}

if (( $# > 1 )); then
    printf 'error: expected at most one output path\n' >&2
    usage >&2
    exit 2
fi

if (( $# == 1 )); then
    case "$1" in
        --print-tools)
            printf 'riscv64-linux-gnu-gcc\ncpio\n'
            exit 0
            ;;
        --print-entries)
            printf '.\ninit\n'
            exit 0
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        -*)
            printf 'error: unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
SOURCE="$SCRIPT_DIR/input_gate_init.c"
OUTPUT="${1:-$REPO_ROOT/target/riscv-xhci/input-gate/initramfs.cpio}"
CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH-0}"
MAX_NEWC_MTIME=4294967295

if [[ ! "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]]; then
    printf 'error: SOURCE_DATE_EPOCH must be a decimal newc u32\n' >&2
    exit 2
fi
NORMALIZED_EPOCH="${SOURCE_DATE_EPOCH#"${SOURCE_DATE_EPOCH%%[!0]*}"}"
NORMALIZED_EPOCH="${NORMALIZED_EPOCH:-0}"
if [[ ${#NORMALIZED_EPOCH} -gt ${#MAX_NEWC_MTIME} ]] ||
    [[ ${#NORMALIZED_EPOCH} -eq ${#MAX_NEWC_MTIME} &&
        "$NORMALIZED_EPOCH" > "$MAX_NEWC_MTIME" ]]; then
    printf 'error: SOURCE_DATE_EPOCH must be a decimal newc u32\n' >&2
    exit 2
fi

if ! command -v "$CC" >/dev/null 2>&1; then
    printf 'error: required compiler not found: %s\n' "$CC" >&2
    exit 1
fi
if ! command -v cpio >/dev/null 2>&1; then
    printf 'error: required tool not found: cpio\n' >&2
    exit 1
fi

OUTPUT_DIR="$(dirname -- "$OUTPUT")"
if [[ -L "$OUTPUT" || -d "$OUTPUT" || -L "$OUTPUT_DIR" ]]; then
    printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
    exit 2
fi
mkdir -p -- "$OUTPUT_DIR"

STAGE="$(mktemp -d)"
OUTPUT_TMP=""
cleanup() {
    if [[ -n "$OUTPUT_TMP" ]]; then
        rm -f -- "$OUTPUT_TMP"
    fi
    rm -rf -- "$STAGE"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"$CC" -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror "$SOURCE" \
    -o "$STAGE/init"
chmod 0755 "$STAGE" "$STAGE/init"
touch -d "@$SOURCE_DATE_EPOCH" "$STAGE" "$STAGE/init"

OUTPUT_TMP="$(mktemp "$OUTPUT_DIR/.initramfs.cpio.tmp.XXXXXX")"
printf '.\ninit\n' |
    cpio --quiet --reproducible --owner=0:0 -o -H newc -D "$STAGE" >"$OUTPUT_TMP"
if [[ ! -s "$OUTPUT_TMP" ]]; then
    printf 'error: generated initramfs is empty\n' >&2
    exit 1
fi
chmod 0644 "$OUTPUT_TMP"
mv -T -- "$OUTPUT_TMP" "$OUTPUT"
OUTPUT_TMP=""
