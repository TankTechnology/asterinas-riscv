#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Compare a freshly built rootfs directory against a board snapshot.
#
#   tools/riscv/debian/rootfs/compare_snapshot.sh <rootfs-dir> <snapshot-dir>
#
# A rebuilt rootfs starts from a profile, so it is never identical to the
# board: this prints what would be missing before the image is written
# anywhere, which is the whole point of taking the snapshot.
#
# Output, sections in the order a rebuild has to address them:
#   packages   in the board snapshot but not in the image (must be installed)
#   units      unit files present on the board but not in the image
#   etc        files present under /etc on the board but not in the image
#
# Only presence is compared, not content: package postinst scripts legitimately
# rewrite configuration, and the board's files are the ones to reconcile by
# hand afterwards. Exit status is 0 when nothing is missing, 1 otherwise.
set -uo pipefail

ROOT=${1:?usage: compare_snapshot.sh <rootfs-dir> <snapshot-dir>}
SNAP=${2:?usage: compare_snapshot.sh <rootfs-dir> <snapshot-dir>}
status=0

WORK_PKG=$(mktemp)
trap 'rm -f "$WORK_PKG" "$WORK_PKG.image"' EXIT

echo "== packages missing from the image"
# `dpkg --get-selections` lines look like "name<TAB>install".
awk '{print $1}' "$SNAP/packages.selections" | sort -u >"$WORK_PKG"
if [ -d "$ROOT/var/lib/dpkg/info" ]; then
    ls "$ROOT/var/lib/dpkg/info" |
        sed 's/\.\(list\|md5sums\|postinst\|preinst\|prerm\|postrm\|conffiles\|triggers\|shlibs\|symbols\|templates\|config\|tlist\|dev\)$//' |
        sort -u >"$WORK_PKG.image"
    missing=$(comm -23 "$WORK_PKG" "$WORK_PKG.image" | wc -l)
    comm -23 "$WORK_PKG" "$WORK_PKG.image" | head -40
    echo "-- $missing package(s) missing"
    [ "$missing" -gt 0 ] && status=1
else
    echo "-- image has no dpkg database; not a rootfs?" >&2
    status=1
fi

echo
echo "== units missing from the image"
if [ -f "$SNAP/units-files.txt" ]; then
    awk '/^-.*\.(service|socket|timer|target|mount)$/ {print $NF}' "$SNAP/units-files.txt" | sort -u >"$WORK_PKG"
    if [ -d "$ROOT/etc/systemd/system" ]; then
        ls "$ROOT/etc/systemd/system" | sort -u >"$WORK_PKG.image"
        unit_missing=$(comm -23 "$WORK_PKG" "$WORK_PKG.image")
        echo "$unit_missing" | sed '/^$/d'
        n=$(echo "$unit_missing" | sed '/^$/d' | wc -l)
        [ "$n" -gt 0 ] && { echo "-- $n unit file(s) missing"; status=1; } || echo "-- none"
    fi
fi

echo
echo "== /etc files missing from the image"
if [ -f "$SNAP/etc-manifest.txt" ]; then
    awk '{print $1}' "$SNAP/etc-manifest.txt" | sed 's|^/||' | sort -u >"$WORK_PKG"
    if [ -d "$ROOT/etc" ]; then
        (cd "$ROOT" && find etc -type f | sort -u) >"$WORK_PKG.image"
        etc_missing=$(comm -23 "$WORK_PKG" "$WORK_PKG.image")
        echo "$etc_missing" | head -40
        n=$(echo "$etc_missing" | sed '/^$/d' | wc -l)
        [ "$n" -gt 0 ] && { echo "-- $n file(s) missing (first 40 shown)"; status=1; } || echo "-- none"
    fi
fi

echo
echo "result: $([ $status -eq 0 ] && echo 'image covers the snapshot' || echo 'delta to reconcile before writing')"
exit "$status"
