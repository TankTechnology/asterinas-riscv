#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

usage() {
    echo "usage: $0 BASE_INITRAMFS OUTPUT_INITRAMFS RUNNER" >&2
    exit 2
}

[[ $# -eq 3 ]] || usage
base=$1
output=$2
runner=$3
[[ -r $base && -r $runner ]] || { echo "base or runner is not readable" >&2; exit 1; }
command -v cpio >/dev/null || { echo "cpio is required" >&2; exit 1; }
command -v gzip >/dev/null || { echo "gzip is required" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
gzip -dc "$base" | (cd "$work" && cpio -idm --quiet)
mkdir -p "$work/benchmark"
cp "$runner" "$work/benchmark/asterinas-benchmark"
chmod 0755 "$work/benchmark/asterinas-benchmark"

cat >"$work/init" <<'EOF'
#!/bin/sh
set -eu
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mkdir -p /ext2
mount -t ext4 /dev/vda /ext2
echo "Asterinas Debian ext4 benchmark ready"
/benchmark/asterinas-benchmark
status=$?
echo "Asterinas benchmark exit status: $status"
poweroff -f
sleep 3600
EOF
chmod 0755 "$work/init"

mkdir -p "$(dirname "$output")"
tmp_output="$output.part"
(cd "$work" && find . -print0 | cpio -o -H newc --null --quiet | gzip -n >"$tmp_output")
mv "$tmp_output" "$output"
