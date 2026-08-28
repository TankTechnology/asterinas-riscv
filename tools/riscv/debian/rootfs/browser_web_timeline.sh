#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly TIMELINE=/home/asterinas/browser-web-timeline.log
readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"

guest_monotonic_ns() {
    # Do not spawn awk in the early-boot critical path.  On Asterinas, an
    # interpreter startup plus a /proc/uptime read can take tens of seconds.
    # Bash exposes EPOCHREALTIME without an extra process; normalise its
    # seconds.microseconds representation to nanoseconds.  The integer-second
    # fallback still gives a non-decreasing value if the shell lacks the
    # newer variable.
    local raw="${EPOCHREALTIME-}"
    if [[ "$raw" =~ ^[0-9]+\.[0-9]{6}$ ]]; then
        raw="${raw/./}"
        printf '%s000' "$raw"
    else
        printf '%s000000000' "${EPOCHSECONDS:-0}"
    fi
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
    printf '%s\n' "$line" >>"$CONSOLE" 2>/dev/null || true
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
