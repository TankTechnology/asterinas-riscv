#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Keep the wired port usable.
#
# The campus switch stops answering this port after roughly an hour of uptime:
# ARP for the gateway gets no reply, the portal is unreachable, and even the
# gateway's unauthenticated HTTP redirect disappears — while broadcast traffic
# keeps arriving, so the link and the driver are fine. A reboot brings the port
# back, which is why bring-up sessions kept "fixing" the network by restarting.
#
# This watchdog probes the gateway and reports the state, so a dead port is a
# timestamped event instead of a surprise. It does not reboot on its own by
# default: a reboot takes the desktop down, and the operator may prefer to
# re-authenticate. Set ASTERINAS_NET_WATCHDOG_REBOOT=1 in
# /etc/asterinas/net-watchdog.conf to let it recover unattended.

set -u

gateway=""
if [ -r /proc/cmdline ]; then
    gateway="$(sed -n 's/.*asterinas\.net=[^,]*,\([0-9.]*\)\/[0-9]*,\([0-9.]*\).*/\2/p' /proc/cmdline)"
fi
# The fallback is an RFC 5737 documentation address, overridable from the
# environment (the guest normally gets the gateway from asterinas.net).
[ -n "$gateway" ] || gateway="${BOARD_GW:-192.0.2.1}"

interval="${ASTERINAS_NET_WATCHDOG_INTERVAL:-30}"
reboot_after="${ASTERINAS_NET_WATCHDOG_REBOOT_AFTER:-20}"
allow_reboot="${ASTERINAS_NET_WATCHDOG_REBOOT:-0}"

failures=0
reported_dead=0

while :; do
    if curl -m 3 -s -o /dev/null "http://${gateway}/" 2>/dev/null; then
        if [ "$reported_dead" -eq 1 ]; then
            printf 'ASTERINAS_NET_WATCHDOG state=recovered gateway=%s after_failures=%s\n' \
                "$gateway" "$failures"
        fi
        failures=0
        reported_dead=0
    else
        failures=$((failures + 1))
        if [ "$failures" -eq 3 ] && [ "$reported_dead" -eq 0 ]; then
            reported_dead=1
            printf 'ASTERINAS_NET_WATCHDOG state=unreachable gateway=%s rx_tx=%s\n' \
                "$gateway" \
                "$(grep eth0 /proc/net/dev 2>/dev/null | tr -s ' ' | cut -d' ' -f2,10)"
        fi
        if [ "$allow_reboot" = "1" ] && [ "$failures" -ge "$reboot_after" ]; then
            printf 'ASTERINAS_NET_WATCHDOG state=rebooting gateway=%s failures=%s\n' \
                "$gateway" "$failures"
            sync
            reboot
            exit 0
        fi
    fi
    sleep "$interval"
done
