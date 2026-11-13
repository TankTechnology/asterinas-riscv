#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Run the crash-consistency gate as a routine check, not only when touching
# the filesystem code.
#
#   tools/riscv/fs/nightly_gate.sh [output-dir]
#
# Two writers, six QEMU kills:
#   * `records` — fsync durability: every acknowledged record survives.
#   * `churn`   — allocation ordering: a crash cannot leave an inode pointing
#                 at a block the bitmap calls free (this is the defect that
#                 took the board's rootfs down twice).
#
# The gate boots its own kernel image in QEMU, so it never touches the board
# and can run while the board is busy. Exit status is the gate's own verdict.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT=${1:-/tmp/fs-gate-$(date +%Y%m%d-%H%M%S)}
ITERATIONS=${GATE_ITERATIONS:-2}

run() {
    local writer=$1 fs=$2 dir="$OUT/$1-$2"
    echo "== crash gate: writer=$writer fs=$fs -> $dir"
    "$HERE/crash_consistency_gate.py" \
        --output-dir "$dir" --writer "$writer" --fs "$fs" --iterations "$ITERATIONS"
}

status=0
run records ext2 || status=1
run churn ext4 || status=1

if [ "$status" -eq 0 ]; then
    echo "crash gate: PASS (records/ext2, churn/ext4, $ITERATIONS iterations each)"
else
    echo "crash gate: FAIL -- see $OUT" >&2
fi
exit "$status"
