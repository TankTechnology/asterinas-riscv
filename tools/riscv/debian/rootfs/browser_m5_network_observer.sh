#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M5_CONSOLE:-/dev/console}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS:-60}"
fail() { printf '%s\n' "DEBIAN_BROWSER_M5_NETNS_FAIL reason=$1" >>"$CONSOLE"; exit 1; }

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))
while :; do
    browser_pid="$(systemctl show --property MainPID --value asterinas-browser-m5.service 2>/dev/null)" || browser_pid=""
    if [[ "$browser_pid" =~ ^[1-9][0-9]*$ && -r "/proc/$browser_pid/ns/net" &&
          "$(cat "/proc/$browser_pid/comm" 2>/dev/null)" == firefox-esr ]]; then
        break
    fi
    ((SECONDS < deadline)) || fail firefox-timeout
    sleep 1
done
initial_ns="$(readlink /proc/self/ns/net)" || fail initial-netns
browser_ns="$(readlink "/proc/$browser_pid/ns/net")" || fail browser-netns
[[ "$initial_ns" != "$browser_ns" ]] || fail firefox-in-initial-netns
[[ -d /sys/class/net/eth0 ]] || fail initial-eth0-missing
printf '%s\n' \
    "DEBIAN_BROWSER_M5_NETNS firefox=private initial=eth0" \
    >>"$CONSOLE"
