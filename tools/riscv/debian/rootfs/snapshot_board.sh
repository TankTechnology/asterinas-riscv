#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Snapshot what is on the board, read-only, so a rebuilt rootfs can be compared
# against it and the delta re-applied.
#
#   tools/riscv/debian/rootfs/snapshot_board.sh [outdir] [ssh-target]
#
# What is captured:
#   packages.selections   `dpkg --get-selections`, the authoritative package set
#   packages.versions     package + version, to detect drift after a rebuild
#   units-enabled.txt     unit files and where they are wanted
#   etc-manifest.txt      every file under /etc with mode and size
#   system.txt            kernel command line, mounts, partitions, sizes
#   root-listing.txt      the scripts and images kept in /root
#
# Everything is read over SSH; nothing is written to the board. The result is
# small enough to keep next to the backup tarball and to diff later.
set -uo pipefail

OUT=${1:-$HOME/megrez-backup-2026-11-14/snapshot}
TARGET=${2:-root@58.206.232.122}
PORT=${BOARD_PORT:-2222}
KEY=${BOARD_KEY:-$HOME/.ssh/asterinas_megrez}

mkdir -p "$OUT"

remote() { # name, command
    local name=$1
    shift
    # shellcheck disable=SC2029
    if timeout 180 ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8 \
        "$TARGET" "$@" >"$OUT/$name" 2>"$OUT/$name.err"; then
        echo "  $name ($(wc -l <"$OUT/$name") lines)"
    else
        echo "  $name FAILED (see $name.err)" >&2
    fi
}

echo "== snapshot of $TARGET -> $OUT"
remote packages.selections 'dpkg --get-selections'
remote packages.versions "dpkg-query -W -f '\${Package} \${Version}\n'"
remote units-files.txt 'ls -la /etc/systemd/system'
remote units-enabled.txt 'systemctl list-unit-files --state=enabled 2>/dev/null || ls /etc/systemd/system/*.wants'
remote etc-manifest.txt "find /etc -type f -printf '%p %m %s\n' 2>/dev/null | sort"
remote system.txt 'sh -c "uname -a; cat /proc/cmdline; cat /proc/partitions; mount; df -h"'
remote root-listing.txt 'ls -la /root'

{
    echo "snapshot: $TARGET"
    echo "taken:    $(date -Is)"
    echo "packages: $(wc -l <"$OUT/packages.selections" 2>/dev/null || echo 0)"
    echo "etc files: $(wc -l <"$OUT/etc-manifest.txt" 2>/dev/null || echo 0)"
} >"$OUT/PROVENANCE.txt"
cat "$OUT/PROVENANCE.txt"
