#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

modprobe virtio_net 2>/dev/null || true
ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up 2>/dev/null || true
route add default gw 10.0.2.2 2>/dev/null || true
echo "IPERF3_CLIENT_READY=10.0.2.15"
output=/ext2/tmp/asterinas-iperf3.json
chroot /ext2 /usr/bin/iperf3 -c 10.0.2.2 -p 15201 -t 10 -O 2 -P 1 -J > "$output"
cat "$output"
sample=$(awk -F: '/"sum_sent"/ { seen=1 } seen && /"bits_per_second"/ { gsub(/[ ,\t]/, "", $2); print $2; exit }' "$output" | tr -d '[:space:]')
[ -n "$sample" ] || exit 1
printf 'PERF_SAMPLE=%s\n' "$sample"
