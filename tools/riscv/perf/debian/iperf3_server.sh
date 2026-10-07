#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up 2>/dev/null || true
echo "IPERF3_SERVER_READY=10.0.2.15"
exec chroot /ext2 /usr/bin/iperf3 -s -B 10.0.2.15 --one-off
