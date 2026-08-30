#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail
umask 077

usage() {
    echo "usage: $0 --expected-crc32 8hex OUTPUT" >&2
    exit 2
}

[[ $# -eq 3 && $1 == --expected-crc32 ]] || usage
EXPECTED_CRC32=$2
OUTPUT=$3
[[ $EXPECTED_CRC32 =~ ^[0-9a-f]{8}$ ]] || {
    echo "error: expected CRC32 must be eight lowercase hexadecimal digits" >&2
    exit 2
}

SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
[[ $SOURCE_DATE_EPOCH =~ ^(0|[1-9][0-9]*)$ ]] || {
    echo "error: SOURCE_DATE_EPOCH must be canonical decimal" >&2
    exit 2
}
(( SOURCE_DATE_EPOCH <= 4294967295 )) || {
    echo "error: SOURCE_DATE_EPOCH exceeds the newc timestamp range" >&2
    exit 2
}

REPOSITORY_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
SOURCE=$REPOSITORY_ROOT/tools/riscv/megrez_sdhci_probe_init.c
RISC_V_CC=${RISC_V_CC:-riscv64-linux-gnu-gcc}
for tool in "$RISC_V_CC" cpio mktemp mv touch chmod; do
    command -v "$tool" >/dev/null || {
        echo "error: required tool is missing: $tool" >&2
        exit 2
    }
done

if [[ -L $OUTPUT || -d $OUTPUT ]]; then
    echo "error: output must not be a symlink or directory" >&2
    exit 2
fi
OUTPUT_NAME=$(basename -- "$OUTPUT")
OUTPUT_DIRECTORY_INPUT=$(dirname -- "$OUTPUT")
mkdir -p -- "$OUTPUT_DIRECTORY_INPUT"
[[ ! -L $OUTPUT_DIRECTORY_INPUT && -d $OUTPUT_DIRECTORY_INPUT ]] || {
    echo "error: output directory is unsafe" >&2
    exit 2
}
OUTPUT_DIRECTORY=$(cd -- "$OUTPUT_DIRECTORY_INPUT" && pwd -P)
OUTPUT=$OUTPUT_DIRECTORY/$OUTPUT_NAME
[[ ! -L $OUTPUT && ! -d $OUTPUT ]] || {
    echo "error: output must not be a symlink or directory" >&2
    exit 2
}

WORK_DIRECTORY=$(mktemp -d)
OUTPUT_TEMP=$(mktemp "$OUTPUT_DIRECTORY/.sdhci-probe.tmp.XXXXXX")
cleanup() {
    [[ -z ${OUTPUT_TEMP:-} ]] || unlink -- "$OUTPUT_TEMP" 2>/dev/null || true
    [[ -z ${WORK_DIRECTORY:-} ]] || find "$WORK_DIRECTORY" -depth -delete 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

STAGE=$WORK_DIRECTORY/stage
mkdir -m 0755 -- "$STAGE"
"$RISC_V_CC" \
    -std=c11 -static -Os -s -Wall -Wextra -Werror \
    "-DEXPECTED_CRC32=0x${EXPECTED_CRC32}U" \
    "$SOURCE" -o "$STAGE/init"
chmod 0755 "$STAGE/init" "$STAGE"
touch -d "@$SOURCE_DATE_EPOCH" "$STAGE/init" "$STAGE"

(
    cd -- "$STAGE"
    printf '.\0init\0' | cpio --null -o --format=newc --owner=0:0 \
        --reproducible --quiet >"$OUTPUT_TEMP"
)
chmod 0644 "$OUTPUT_TEMP"
mv -T -- "$OUTPUT_TEMP" "$OUTPUT"
OUTPUT_TEMP=
