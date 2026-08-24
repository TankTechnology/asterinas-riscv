#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: build_stage1.sh [OUTPUT]
       build_stage1.sh --print-tools
       build_stage1.sh --print-entries

Build the static RISC-V Debian root-handoff initramfs.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

if (( $# > 1 )); then
    die_usage "expected at most one output path"
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
            die_usage "unknown option: $1"
            ;;
    esac
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../../../.." && pwd)"
SOURCE="$SCRIPT_DIR/stage1_init.c"
OUTPUT="${1:-$REPOSITORY_ROOT/target/debian-riscv/stage1/initramfs.cpio}"
COMPILER="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH-0}"
MAX_NEWC_TIMESTAMP=4294967295

if [[ ! "$SOURCE_DATE_EPOCH" =~ ^(0|[1-9][0-9]*)$ ]] ||
    [[ ${#SOURCE_DATE_EPOCH} -gt ${#MAX_NEWC_TIMESTAMP} ]] ||
    { [[ ${#SOURCE_DATE_EPOCH} -eq ${#MAX_NEWC_TIMESTAMP} ]] &&
        [[ "$SOURCE_DATE_EPOCH" > "$MAX_NEWC_TIMESTAMP" ]]; }; then
    printf 'error: SOURCE_DATE_EPOCH must be a canonical decimal newc u32\n' >&2
    exit 2
fi

if ! command -v "$COMPILER" >/dev/null 2>&1; then
    printf 'error: required compiler not found: %s\n' "$COMPILER" >&2
    exit 1
fi
if ! command -v cpio >/dev/null 2>&1; then
    printf 'error: required tool not found: cpio\n' >&2
    exit 1
fi

case "$OUTPUT" in
    '' | */ | *$'\n'* | *$'\r'* | *$'\t'*)
        printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
        exit 2
        ;;
esac

OUTPUT_DIRECTORY="$(dirname -- "$OUTPUT")"
OUTPUT_BASENAME="$(basename -- "$OUTPUT")"
INIT_OUTPUT="$OUTPUT_DIRECTORY/init"
if [[ "$OUTPUT_BASENAME" == "init" || "$OUTPUT" == "$SOURCE" ||
    -L "$OUTPUT" || -d "$OUTPUT" || -L "$INIT_OUTPUT" ||
    -d "$INIT_OUTPUT" ]]; then
    printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
    exit 2
fi

require_safe_directory_chain() {
    local directory="$1"
    local current=""
    local component
    local -a components

    if [[ "$directory" == /* ]]; then
        current="/"
    fi
    IFS='/' read -r -a components <<<"$directory"
    for component in "${components[@]}"; do
        [[ -n "$component" ]] || continue
        if [[ "$component" == "." || "$component" == ".." ]]; then
            printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
            exit 2
        fi
        if [[ "$current" == "/" ]]; then
            current="/$component"
        elif [[ -n "$current" ]]; then
            current="$current/$component"
        else
            current="$component"
        fi
        if [[ -L "$current" || ( -e "$current" && ! -d "$current" ) ]]; then
            printf 'error: unsafe output path: %s\n' "$OUTPUT" >&2
            exit 2
        fi
    done
}

require_safe_directory_chain "$OUTPUT_DIRECTORY"
umask 022
mkdir -p -- "$OUTPUT_DIRECTORY"
require_safe_directory_chain "$OUTPUT_DIRECTORY"

STAGE="$(mktemp -d)"
ARCHIVE_TEMP=""
INIT_TEMP=""
cleanup() {
    if [[ -n "$ARCHIVE_TEMP" ]]; then
        rm -f -- "$ARCHIVE_TEMP"
    fi
    if [[ -n "$INIT_TEMP" ]]; then
        rm -f -- "$INIT_TEMP"
    fi
    rm -rf -- "$STAGE"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"$COMPILER" -std=c11 -O2 -static -no-pie -Wall -Wextra -Werror \
    "$SOURCE" -o "$STAGE/init"
chmod 0755 "$STAGE" "$STAGE/init"
touch -d "@$SOURCE_DATE_EPOCH" "$STAGE" "$STAGE/init"

ARCHIVE_TEMP="$(mktemp "$OUTPUT_DIRECTORY/.initramfs.cpio.tmp.XXXXXX")"
printf '.\ninit\n' |
    cpio --quiet --reproducible --owner=0:0 --create --format=newc \
        --directory="$STAGE" >"$ARCHIVE_TEMP"
if [[ ! -s "$ARCHIVE_TEMP" ]]; then
    printf 'error: generated initramfs is empty\n' >&2
    exit 1
fi

ARCHIVE_ENTRIES="$(cpio --quiet --list <"$ARCHIVE_TEMP")"
if [[ "$ARCHIVE_ENTRIES" != $'.\ninit' ]]; then
    printf 'error: generated initramfs has unexpected entries\n' >&2
    exit 1
fi
chmod 0644 "$ARCHIVE_TEMP"

INIT_TEMP="$(mktemp "$OUTPUT_DIRECTORY/.init.tmp.XXXXXX")"
cp -- "$STAGE/init" "$INIT_TEMP"
chmod 0755 "$INIT_TEMP"

# The archive is the commit artifact; publish its matching standalone ELF first.
mv -T -- "$INIT_TEMP" "$INIT_OUTPUT"
INIT_TEMP=""
mv -T -- "$ARCHIVE_TEMP" "$OUTPUT"
ARCHIVE_TEMP=""
