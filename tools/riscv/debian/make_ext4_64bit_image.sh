#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Create a 64-bit-descriptor variant of an existing ext4 test image.
#
# The base image must already use extents and an internal journal.  The helper
# keeps the old image untouched, converts the copy with resize2fs, repairs it
# with e2fsck, and verifies the features the ext4 gate relies on.

set -euo pipefail
umask 077

die() {
    printf 'make_ext4_64bit_image: %s\n' "$*" >&2
    exit 2
}

if [[ $# != 2 ]]; then
    die "usage: $0 <ext4-base.img> <ext4-64bit.img>"
fi
readonly BASE=$1
readonly OUTPUT=$2

[[ -f "$BASE" ]] || die "base image not found: $BASE"
for tool in cp resize2fs e2fsck dumpe2fs; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

mkdir -p -- "$(dirname -- "$OUTPUT")"
cp --sparse=always -- "$BASE" "$OUTPUT"

features="$(dumpe2fs -h "$OUTPUT" 2>/dev/null | sed -n 's/^Filesystem features:[[:space:]]*//p')"
[[ "$features" == *has_journal* ]] || die "base image has no internal journal"
[[ "$features" == *extent* ]] || die "base image does not use extents"

if [[ "$features" != *64bit* ]]; then
    resize2fs -b "$OUTPUT" >/dev/null 2>&1 || die "resize2fs -b failed"
fi

set +e
e2fsck -fy "$OUTPUT" >/dev/null 2>&1
status=$?
set -e
((status <= 1)) || die "e2fsck -fy failed with status $status"

features="$(dumpe2fs -h "$OUTPUT" 2>/dev/null | sed -n 's/^Filesystem features:[[:space:]]*//p')"
for required in has_journal extent 64bit; do
    [[ "$features" == *"$required"* ]] || die "result is missing the $required feature"
done
descriptor_size="$(dumpe2fs -h "$OUTPUT" 2>/dev/null | sed -n 's/^Group descriptor size:[[:space:]]*//p')"
[[ "$descriptor_size" == 64 ]] || die "group descriptor size is not 64"
printf 'EXT4_64BIT_IMAGE output=%s descriptor_size=%s features=%s\n' \
    "$OUTPUT" "$descriptor_size" "${features// /,}"
