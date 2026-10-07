#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

mode=${1:?fio mode is required}
case "$mode" in
    read|write|randread|randwrite) ;;
    *) echo "unsupported fio mode: $mode" >&2; exit 2 ;;
esac

mkdir -p /var/lib/asterinas-perf
output=$(mktemp /tmp/asterinas-fio.XXXXXX)
trap 'rm -f "$output"' EXIT
/usr/bin/fio \
    --name=debian-ext4-${mode} \
    --filename=/var/lib/asterinas-perf/fio-test \
    --size=256M --rw="$mode" --bs=1M --iodepth=1 \
    --direct=1 --runtime=10 --time_based --group_reporting \
    --output-format=json --output="$output"
cat "$output"
case "$mode" in
    read|randread)
        bw_bytes=$(awk -F: '/"read"[[:space:]]*:/ { seen=1 } seen && /"bw_bytes"/ { gsub(/[ ,]/, "", $2); print $2; exit }' "$output")
        ;;
    write|randwrite)
        bw_bytes=$(awk -F: '/"write"[[:space:]]*:/ { seen=1 } seen && /"bw_bytes"/ { gsub(/[ ,]/, "", $2); print $2; exit }' "$output")
        ;;
esac
[ -n "$bw_bytes" ] || { echo "fio JSON did not contain bw_bytes" >&2; exit 1; }
printf 'PERF_SAMPLE=%s\n' "$bw_bytes"
