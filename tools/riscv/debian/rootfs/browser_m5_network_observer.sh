#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M5_CONSOLE:-/dev/console}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS:-300}"
readonly PROC_ROOT="${ASTERINAS_BROWSER_M5_PROC_ROOT:-/proc}"
fail() { printf '%s\n' "DEBIAN_BROWSER_M5_NETNS_FAIL reason=$1" >>"$CONSOLE"; exit 1; }

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))
while :; do
    browser_pid="$(systemctl show --property MainPID --value asterinas-browser-m5.service 2>/dev/null)" || browser_pid=""
    if [[ "$browser_pid" =~ ^[1-9][0-9]*$ && -r "$PROC_ROOT/$browser_pid/ns/net" &&
          "$(cat "$PROC_ROOT/$browser_pid/comm" 2>/dev/null)" == firefox-esr ]]; then
        break
    fi
    ((SECONDS < deadline)) || fail firefox-timeout
    sleep 1
done
initial_ns="$(readlink "$PROC_ROOT/self/ns/net")" || fail initial-netns
browser_ns="$(readlink "$PROC_ROOT/$browser_pid/ns/net")" || fail browser-netns
[[ "$initial_ns" != "$browser_ns" ]] || fail firefox-in-initial-netns
printf '%s\n' \
    "DEBIAN_BROWSER_M5_NETNS firefox=private initial=distinct" \
    >>"$CONSOLE"
