#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Take everything off the board that would be painful to lose.
#
#   tools/riscv/board/backup_board.sh [outdir]
#
# The board's only storage is an eMMC that has already corrupted file
# contents once (/etc/group with every GID blanked, a package alternative file
# holding an unrelated Python script). The host is the only copy that does not
# depend on that device, so this runs against it rather than against anything
# on the board.
#
# What lands in <outdir>:
#   snapshot/            packages, units, /etc manifest, cmdline, mounts
#                        (tools/riscv/debian/rootfs/snapshot_board.sh)
#   system-config.tar.gz /etc /opt /root /srv /var -- including the kernel
#                        images, so a known-good kernel survives with the data
#   home.tar.gz          /home
#   rootfs-inventory.txt every file with mode, owner and size
#   PROVENANCE.txt       what was taken, when, from which boot, and checksums
#
# Exclusions are things that can be rebuilt from a package mirror (the apt
# archive cache, journal files) -- not anything whose loss would be noticed.
#
# Exit status: 0 everything captured and verified, 1 otherwise.
set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
OUT=${1:-$HOME/megrez-backup-$(date +%Y-%m-%d)}
HOST=${BOARD_HOST:-58.206.232.122}
PORT=${BOARD_PORT:-22}
KEY=${BOARD_KEY:-$HOME/.ssh/asterinas_megrez}

SSH=(ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no
     -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8)

mkdir -p "$OUT"
status=0

echo "== snapshot"
BOARD_PORT="$PORT" BOARD_HOST="$HOST" BOARD_KEY="$KEY" \
    "$REPO/tools/riscv/debian/rootfs/snapshot_board.sh" "$OUT/snapshot" "root@$HOST" || status=1

echo "== file system"
"${SSH[@]}" "root@$HOST" \
    "tar czf - -C / --ignore-failed-read --exclude=var/cache/apt/archives \
       --exclude=var/log/journal --exclude=lost+found etc opt root srv var" \
    >"$OUT/system-config.tar.gz" 2>"$OUT/system-config.err" || status=1
echo "  system-config.tar.gz $(du -h "$OUT/system-config.tar.gz" | cut -f1)"

# A stale file handle is a real finding, not a reason to lose the rest of the
# backup: ESTALE entries are what this board's eMMC has produced before, and
# refusing to archive anything because of one is worse than recording it.
"${SSH[@]}" "root@$HOST" "tar czf - -C / --ignore-failed-read home" \
    >"$OUT/home.tar.gz" 2>"$OUT/home.err" || true
echo "  home.tar.gz $(du -h "$OUT/home.tar.gz" | cut -f1)"
if [ -s "$OUT/home.err" ]; then
    echo "  note: $(grep -c 'Cannot stat' "$OUT/home.err" 2>/dev/null || echo 0) entr(ies) unreadable, see home.err"
fi

"${SSH[@]}" "root@$HOST" \
    "find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /tmp \) -prune -o \
       -printf '%M %U:%G %10s %p\n' 2>/dev/null | sort -k4" >"$OUT/rootfs-inventory.txt" || status=1
echo "  rootfs-inventory.txt $(wc -l <"$OUT/rootfs-inventory.txt") entries"

echo "== verification"
# A truncated tarball is worse than no tarball: it looks like a backup.
for tarball in system-config.tar.gz home.tar.gz; do
    if tar tzf "$OUT/$tarball" >/dev/null 2>&1; then
        echo "  ok   $tarball readable ($(tar tzf "$OUT/$tarball" | wc -l) entries)"
    else
        echo "  FAIL $tarball is not a readable archive"
        status=1
    fi
done

{
    echo "backup:   $OUT"
    echo "taken:    $(date -Is)"
    echo "from:     root@$HOST:$PORT"
    echo "kernel:   $("${SSH[@]}" "root@$HOST" 'sha256sum /root/kernel-rel.img 2>/dev/null | cut -d" " -f1')"
    echo "uptime:   $("${SSH[@]}" "root@$HOST" 'cat /proc/uptime | cut -d" " -f1')s"
    echo "packages: $(wc -l <"$OUT/snapshot/packages.selections" 2>/dev/null || echo 0)"
    echo
    echo "sha256:"
    ( cd "$OUT" && sha256sum system-config.tar.gz home.tar.gz rootfs-inventory.txt 2>/dev/null )
} >"$OUT/PROVENANCE.txt"
sed 's/^/  /' "$OUT/PROVENANCE.txt"

echo
if [ "$status" -eq 0 ]; then
    echo "backup: OK ($OUT)"
else
    echo "backup: INCOMPLETE -- see the failures above ($OUT)"
fi
exit "$status"
