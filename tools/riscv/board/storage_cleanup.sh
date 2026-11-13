#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Reclaim space on the Megrez root filesystem. Dry-run by default.
#
#   tools/riscv/board/storage_cleanup.sh              # show what would go
#   tools/riscv/board/storage_cleanup.sh --apply      # remove it
#   tools/riscv/board/storage_cleanup.sh --apply --orphans   # also /lost+found
#
# What it removes, and why each is safe:
#   /var/lib/apt/lists/*      package lists, rebuilt by `apt-get update`
#   /var/cache/apt/archives/* downloaded .deb files, re-downloadable
#   /tmp/*                    tmpfs on this board; nothing is on disk
#   /lost+found/*             orphans left by e2fsck; opt-in with --orphans
#
# What it never touches, even with --apply:
#   /home, /usr, /etc, /var/lib/dpkg, the git worktree, and any file that is
#   not on the list above. Kernel images in /root are listed with sizes so the
#   operator can decide; the script does not delete them.
#
# Every step is printed before it runs; with `sync` before each removal so a
# bad eMMC moment cannot take dirty data with it.
set -uo pipefail

KEY=${BOARD_KEY:-$HOME/.ssh/asterinas_megrez}
PORT=${BOARD_PORT:-2222}
TARGET=${BOARD_TARGET:-root@58.206.232.122}
LOG=${CLEANUP_LOG:-/tmp/tp/storage-cleanup-$(date +%Y%m%d-%H%M%S).log}

APPLY=0
ORPHANS=0
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --orphans) ORPHANS=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$(dirname "$LOG")"
ssh_board() {
    # shellcheck disable=SC2029
    timeout 300 ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8 \
        "$TARGET" "$1" 2>&1 | tee -a "$LOG"
}

if ! ssh_board 'true' >/dev/null; then
    echo "board not reachable" >&2
    exit 1
fi

echo "== before" | tee -a "$LOG"
ssh_board 'df -h / | tail -1'

report() { # label, size command
    local label=$1 size
    size=$(ssh_board "$2" | tail -1)
    printf '%-28s %s\n' "$label" "$size" | tee -a "$LOG"
}

echo "== reclaimable" | tee -a "$LOG"
report 'apt lists'      'du -sh /var/lib/apt/lists 2>/dev/null | cut -f1'
report 'apt archives'   'du -sh /var/cache/apt/archives 2>/dev/null | cut -f1'
report '/lost+found'    'du -sh /lost+found 2>/dev/null | cut -f1'
report '/usr/share/doc' 'du -sh /usr/share/doc 2>/dev/null | cut -f1'
echo "-- kernel images in /root (not deleted by this script):" | tee -a "$LOG"
ssh_board 'ls -lh /root/*.img 2>/dev/null' | tee -a "$LOG"

if [ "$APPLY" -eq 0 ]; then
    cat <<EOF | tee -a "$LOG"

dry run: nothing was removed. Re-run with --apply to execute:
  sync; rm -rf /var/lib/apt/lists/*
  sync; rm -rf /var/cache/apt/archives/*.deb
  sync; rm -rf /tmp/*            # tmpfs, frees RAM only
$( [ "$ORPHANS" -eq 1 ] && echo '  sync; rm -rf /lost+found/*' )
EOF
    exit 0
fi

echo "== applying" | tee -a "$LOG"
ssh_board 'sync; rm -rf /var/lib/apt/lists/*; echo lists-cleared'
ssh_board 'sync; rm -rf /var/cache/apt/archives/*.deb; echo archives-cleared'
ssh_board 'sync; rm -rf /tmp/*; echo tmp-cleared'
if [ "$ORPHANS" -eq 1 ]; then
    ssh_board 'sync; rm -rf /lost+found/*; echo orphans-cleared'
fi

echo "== after" | tee -a "$LOG"
ssh_board 'sync; df -h / | tail -1'
ssh_board 'dmesg | grep -iE "mmc.*(error|failed)|ioerror" | tail -5'
echo "log: $LOG"
