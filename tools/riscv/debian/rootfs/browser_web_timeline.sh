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
    # The timeline file is the authoritative evidence.  A non-root browser
    # service may not have permission to write /dev/console on Asterinas;
    # never let that optional mirror prevent the phase from completing.
    printf '%s\n' "$line" >>"$CONSOLE" 2>/dev/null || true
}

case "${1-}" in
    begin)
        : >"$TIMELINE"
        # The rootfs builder pre-creates this file as uid 1000.  Preserve
        # that ownership while truncating it; Firefox can then append without
        # relying on runtime chown support in Asterinas.
        chmod 0600 "$TIMELINE"
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
