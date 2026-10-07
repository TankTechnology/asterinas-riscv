#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

usage() {
    echo "usage: $0 BASE_INITRAMFS OUTPUT_INITRAMFS BENCHMARK [--no-block]" >&2
    exit 2
}

[[ $# -ge 3 && $# -le 4 ]] || usage
base=$1
output=$2
benchmark=$3
mount_block=1
if [[ ${4:-} == --no-block ]]; then
    mount_block=0
elif [[ $# -eq 4 ]]; then
    usage
fi
[[ -r $base ]] || { echo "base initramfs is not readable: $base" >&2; exit 1; }
[[ $benchmark = /* ]] || { echo "benchmark must be an absolute path" >&2; exit 1; }
command -v cpio >/dev/null || { echo "cpio is required" >&2; exit 1; }
command -v gzip >/dev/null || { echo "gzip is required" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
gzip -dc "$base" | (cd "$work" && cpio -idm --quiet)

cat >"$work/init" <<EOF
#!/bin/sh
set -eu
mount -t proc proc /proc
mount -t sysfs sysfs /sys
if [ "$mount_block" -eq 1 ]; then
    mount -t devtmpfs devtmpfs /dev
    mkdir -p /ext2
    mount -t ext2 /dev/vda /ext2
fi
echo "Linux initramfs ready"
"$benchmark"
status=\$?
echo "Linux benchmark exit status: \$status"
poweroff -f
halt -f
sleep 3600
EOF
chmod 0755 "$work/init"

mkdir -p "$(dirname "$output")"
tmp_output="$output.part"
(cd "$work" && find . -print0 | cpio -o -H newc --null --quiet | gzip -n >"$tmp_output")
mv "$tmp_output" "$output"
