#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly TIMELINE=/home/asterinas/browser-web-timeline.log
readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"

guest_monotonic_ns() {
    # Use Bash's built-in read so early boot does not spawn an interpreter,
    # while preserving the same CLOCK_MONOTONIC domain used by Python.
    local raw seconds fraction ignored
    IFS=' ' read -r raw ignored </proc/uptime
    [[ "$raw" =~ ^([0-9]+)\.([0-9]+)$ ]] || return 1
    seconds="${BASH_REMATCH[1]}"
    fraction="${BASH_REMATCH[2]}000000000"
    printf '%s%s' "$seconds" "${fraction:0:9}"
}

marker() {
    local name="$1" pid="${2:-0}" page="${3:-}" line guest_ns
    guest_ns="$(guest_monotonic_ns)"
    line="A_WEB_TIMELINE marker=$name guest_monotonic_ns=$guest_ns firefox_pid=$pid"
    [[ -z "$page" ]] || line="$line page=$page"
    printf '%s\n' "$line" >>"$TIMELINE"
    # The browser phases run as the unprivileged asterinas user.  Asterinas
    # may intentionally reject writes to /dev/console from that uid; console
    # output is diagnostic only and must never restart the browser service.
    # The timeline is authoritative in the per-user file; console output is
    # diagnostic only and may be denied for uid 1000 on Asterinas.
    (printf '%s\n' "$line" >>"$CONSOLE") 2>/dev/null || true
}

case "${1-}" in
    begin)
        # The file is provisioned with its final owner in the immutable image.
        # Avoid invoking install/chown during early boot: Asterinas may block
        # that metadata path while sysinit is still bringing up the desktop.
        : >"$TIMELINE"
        marker BOOT_SYSTEMD_BEGIN
        ;;
    basic)
        marker BOOT_BASIC_TARGET
        ;;
    wait-x)
        marker BOOT_NETWORK_READY
        deadline=$((SECONDS + 300))
        # A stale X11 socket can survive a failed Xorg start.  Treat the
        # socket as a hint only; xdpyinfo must complete against the live
        # server before Firefox is allowed to exec.
        while :; do
            if [[ -S /tmp/.X11-unix/X0 ]] &&
                /usr/bin/timeout 5 /usr/bin/xdpyinfo -display "${DISPLAY:-:0}" >/dev/null 2>&1; then
                marker BOOT_X_SOCKET_READY
                exit 0
            fi
            if ((SECONDS >= deadline)); then
                printf '%s\n' 'ASTERINAS_BROWSER_WEB wait-x failed: bounded xdpyinfo probe did not succeed' >&2
                exit 1
            fi
            sleep 1
        done
        ;;
    *)
        exit 2
        ;;
esac
