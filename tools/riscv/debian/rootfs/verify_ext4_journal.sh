#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

usage() {
    printf 'usage: %s --image IMAGE --manifest MANIFEST --packages-lock LOCK\n' "$0" >&2
    exit 2
}

image=''
manifest=''
packages_lock=''
while (($# > 0)); do
    case "$1" in
        --image|--manifest|--packages-lock)
            (($# >= 2)) || usage
            case "$1" in
                --image) image="$2" ;;
                --manifest) manifest="$2" ;;
                --packages-lock) packages_lock="$2" ;;
            esac
            shift 2
            ;;
        *) usage ;;
    esac
done

[[ -n "$image" && -n "$manifest" && -n "$packages_lock" ]] || usage
[[ -f "$image" && -f "$manifest" && -f "$packages_lock" ]] || {
    printf 'all inputs must be regular files\n' >&2
    exit 1
}

features="$(dumpe2fs -h "$image" 2>/dev/null | sed -n 's/^Filesystem features:[[:space:]]*//p')"
[[ "$features" == *' has_journal '* || "$features" == has_journal* || "$features" == *' has_journal' ]] || {
    printf 'ext4 journal feature is missing\n' >&2
    exit 1
}
for forbidden in metadata_csum 64bit flex_bg orphan_file; do
    [[ "$features" != *"$forbidden"* ]] || {
        printf 'unsupported ext4 feature is present: %s\n' "$forbidden" >&2
        exit 1
    }
done

PYTHONPATH="$(cd -- "$(dirname -- "$0")/../../../.." && pwd -P)" \
    python3 -m tools.riscv.debian.rootfs.contract verify \
    --image "$image" --manifest "$manifest" --packages-lock "$packages_lock"
printf 'EXT4_JOURNAL_CONTRACT_PASS image=%s\n' "$image"
