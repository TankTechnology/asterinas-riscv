#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M3_CONSOLE:-/dev/console}"
readonly INPUT_DIRECTORY="${ASTERINAS_DESKTOP_M3_INPUT_DIRECTORY:-/dev/input}"
readonly XORG_LOG="${ASTERINAS_DESKTOP_M3_XORG_LOG:-/home/asterinas/Xorg.0.log}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M3_TIMEOUT_SECONDS:-120}"
readonly USER_NAME="asterinas"
readonly USER_ID="1000"

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "DEBIAN_DESKTOP_M3_FAIL reason=$1"
    exit 1
}

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))

has_input_property() {
    local property="$1"
    local event
    local properties

    for event in "$INPUT_DIRECTORY"/event*; do
        [[ -e "$event" ]] || continue
        properties="$(udevadm info --query=property --name="$event" 2>/dev/null)" ||
            continue
        [[ "$properties" == *"$property=1"* ]] && return 0
    done
    return 1
}

ready() {
    local sessions

    systemctl is-active --quiet systemd-udevd.service || return 1
    systemctl is-active --quiet systemd-logind.service || return 1
    sessions="$(loginctl list-sessions --no-legend 2>/dev/null)" || return 1
    [[ "$sessions" =~ (^|[[:space:]])$USER_NAME([[:space:]]|$) ]] || return 1
    has_input_property ID_INPUT_KEYBOARD || return 1
    if ! has_input_property ID_INPUT_MOUSE &&
        ! has_input_property ID_INPUT_TOUCHSCREEN; then
        return 1
    fi
    [[ -f "$XORG_LOG" ]] || return 1
    grep -q 'FBDEV(0)' "$XORG_LOG" || return 1
    grep -q 'Adding extended input device.*keyboard' "$XORG_LOG" || return 1
    grep -q 'Adding extended input device.*pointer' "$XORG_LOG" || return 1
    pgrep -u "$USER_ID" -f '(^|/)matchbox-window-manager([[:space:]]|$)' \
        >/dev/null || return 1
    pgrep -u "$USER_ID" -x xterm >/dev/null || return 1
}

while ! ready; do
    ((SECONDS < deadline)) || fail desktop-timeout
    sleep 1
done

emit "DEBIAN_DESKTOP_M3_READY user=$USER_NAME display=:0"
