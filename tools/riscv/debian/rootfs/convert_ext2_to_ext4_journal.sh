#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail
umask 077

usage() {
    printf 'usage: %s --input EXT2_IMAGE --output EXT4_IMAGE\n' "$0" >&2
    exit 2
}

input=''
output=''
while (($# > 0)); do
    case "$1" in
        --input)
            (($# >= 2)) || usage
            input="$2"
            shift 2
            ;;
        --output)
            (($# >= 2)) || usage
            output="$2"
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

[[ -n "$input" && -n "$output" && "$input" != "$output" ]] || usage
[[ -f "$input" ]] || { printf 'input image is not a regular file: %s\n' "$input" >&2; exit 1; }
[[ ! -e "$output" ]] || { printf 'refusing to overwrite output: %s\n' "$output" >&2; exit 1; }

command -v e2fsck >/dev/null || { printf 'missing e2fsck\n' >&2; exit 1; }
command -v tune2fs >/dev/null || { printf 'missing tune2fs\n' >&2; exit 1; }
command -v dumpe2fs >/dev/null || { printf 'missing dumpe2fs\n' >&2; exit 1; }

input_type="$(file -b -- "$input" 2>/dev/null || true)"
[[ "$input_type" == *'ext2 filesystem data'* || "$input_type" == *'ext3 filesystem data'* ]] || {
    printf 'input is not an ext2-compatible filesystem image: %s\n' "$input" >&2
    exit 1
}

e2fsck -fn "$input" >/dev/null
mkdir -p -- "$(dirname -- "$output")"
cp --reflink=auto -- "$input" "$output"

# Retain the ext2 inode/block layout. This creates a journal without enabling
# ext4-only extents or metadata checksums, the subset understood by the
# Asterinas ext4 compatibility mount.
tune2fs -j "$output" >/dev/null
e2fsck -fn "$output" >/dev/null
dumpe2fs -h "$output" 2>/dev/null | grep -Eq '^Filesystem features:.*(^|[[:space:]])has_journal([[:space:]]|$)'

printf 'EXT4_JOURNAL_IMAGE_PASS input=%s output=%s\n' "$input" "$output"
