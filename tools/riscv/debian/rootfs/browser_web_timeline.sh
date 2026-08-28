#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly TIMELINE=/home/asterinas/browser-web-timeline.log
readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"

marker() {
    local name="$1" pid="${2:-0}" page="${3:-}" line
    line="A_WEB_TIMELINE marker=$name guest_monotonic_ns=$(awk '{printf \"%.0f\", $1 * 1000000000}' /proc/uptime) firefox_pid=$pid"
    [[ -z "$page" ]] || line="$line page=$page"
    printf '%s\n' "$line" >>"$TIMELINE"
    printf '%s\n' "$line" >>"$CONSOLE"
}

case "${1-}" in
    begin)
        install -o 1000 -g 1000 -m 0600 /dev/null "$TIMELINE"
        marker BOOT_SYSTEMD_BEGIN
        ;;
    basic)
        marker BOOT_BASIC_TARGET
        ;;
    wait-x)
        marker BOOT_NETWORK_READY
        deadline=$((SECONDS + 300))
        while [[ ! -S /tmp/.X11-unix/X0 ]]; do
            ((SECONDS < deadline)) || exit 1
            sleep 1
        done
        marker BOOT_X_SOCKET_READY
        ;;
    *)
        exit 2
        ;;
esac
