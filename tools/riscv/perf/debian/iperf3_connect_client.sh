#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

modprobe virtio_net 2>/dev/null || true
ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up 2>/dev/null || true
route add default gw 10.0.2.2 2>/dev/null || true
start=$(chroot /ext2 /usr/bin/date +%s%N)
set +e
chroot /ext2 /usr/bin/iperf3 -c 10.0.2.2 -p 15201 -t 1 -J >/ext2/tmp/asterinas-iperf3-connect.json
status=$?
set -e
end=$(chroot /ext2 /usr/bin/date +%s%N)
cat /ext2/tmp/asterinas-iperf3-connect.json
elapsed_us=$(( (end - start) / 1000 ))
[ "$elapsed_us" -gt 0 ] || elapsed_us=1
echo "IPERF3_EXIT=$status"
printf 'PERF_SAMPLE=%s\n' "$elapsed_us"
[ "$status" -eq 0 ]
