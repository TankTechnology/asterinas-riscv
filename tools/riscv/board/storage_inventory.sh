#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Read-only storage inventory for the Megrez board.
#
#   tools/riscv/board/storage_inventory.sh [logfile]
#
# Everything here only reads: partition table, mounts, `df`, `du` of the
# directories a cleanup would touch, the kernel log's I/O errors, and the
# sizes of the files that are candidates for removal. It never writes to the
# filesystem, so it is safe to run the moment the board answers.
#
# The Linux side names the eMMC `mmcblk1` where Asterinas names it
# `mmcblk0`; this script sticks to filesystem-level queries so it can run
# under either.
set -uo pipefail

KEY=${BOARD_KEY:-$HOME/.ssh/asterinas_megrez}
PORT=${BOARD_PORT:-2222}
TARGET=${BOARD_TARGET:-root@58.206.232.122}
LOG=${1:-/tmp/tp/storage-inventory-$(date +%Y%m%d-%H%M%S).log}
mkdir -p "$(dirname "$LOG")"

run() {
    echo "### $*" | tee -a "$LOG"
    # shellcheck disable=SC2029
    timeout 120 ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8 \
        "$TARGET" "$*" 2>&1 | tee -a "$LOG"
    echo | tee -a "$LOG"
}

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if ! "$HERE/board_ssh_ensure.sh" 120; then
    echo "board not reachable; nothing to inventory" >&2
    exit 1
fi

run 'df -h'
run 'cat /proc/partitions'
run 'mount'
run 'du -sm /var /var/lib/apt/lists /var/cache/apt/archives /tmp /lost+found /root /home 2>/dev/null'
run 'ls -l /lost+found | head -20'
run 'ls -l /root/*.img /root/*.bak 2>/dev/null'
run 'dmesg | grep -iE "mmc|block.*error|ioerror|ext2|estale" | tail -30'
run 'dpkg --audit 2>&1 | head'
run 'ls -l /var/lib/apt/extended_states 2>&1'
run 'uptime'

echo "inventory written to $LOG"
