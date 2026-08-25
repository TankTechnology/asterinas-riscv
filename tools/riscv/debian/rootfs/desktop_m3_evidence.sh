#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M3_CONSOLE:-/dev/console}"
readonly INPUT_DIRECTORY="${ASTERINAS_DESKTOP_M3_INPUT_DIRECTORY:-/dev/input}"
readonly XORG_LOG="${ASTERINAS_DESKTOP_M3_XORG_LOG:-/home/asterinas/Xorg.0.log}"
readonly SESSION_LOG="${ASTERINAS_DESKTOP_M3_SESSION_LOG:-/home/asterinas/desktop-m3-session.log}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M3_TIMEOUT_SECONDS:-240}"
readonly USER_NAME="asterinas"
readonly USER_ID="1000"

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    local reason="$1"
    systemctl --no-pager --full status \
        dbus.service \
        systemd-logind.service \
        asterinas-desktop-m3.service \
        >>"$CONSOLE" 2>&1 || true
    printf '%s\n' '--- desktop runtime objects ---' >>"$CONSOLE"
    stat -c 'path=%n uid=%u gid=%g mode=%a type=%F' \
        /run/dbus/system_bus_socket \
        /run/systemd/journal/socket \
        /run/systemd/journal/stdout \
        /tmp/.X11-unix/X0 \
        "$INPUT_DIRECTORY"/event* \
        >>"$CONSOLE" 2>&1 || true
    for event in "$INPUT_DIRECTORY"/event*; do
        [[ -e "$event" ]] || continue
        udevadm info --query=property --name="$event" >>"$CONSOLE" 2>&1 || true
    done
    if [[ -f "$SESSION_LOG" ]]; then
        printf '%s\n' '--- desktop session log ---' >>"$CONSOLE"
        cat -- "$SESSION_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    if [[ -f "$XORG_LOG" ]]; then
        printf '%s\n' '--- Xorg log ---' >>"$CONSOLE"
        cat -- "$XORG_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    emit "DEBIAN_DESKTOP_M3_FAIL reason=$reason"
    exit 1
}

[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))

ready() {
    local sessions
    local window_tree

    systemctl is-active --quiet systemd-udevd.service || return 1
    systemctl is-active --quiet systemd-logind.service || return 1
    sessions="$(loginctl list-sessions --no-legend 2>/dev/null)" || return 1
    [[ "$sessions" =~ (^|[[:space:]])$USER_NAME([[:space:]]|$) ]] || return 1
    [[ -e "$INPUT_DIRECTORY/event0" ]] || return 1
    [[ -e "$INPUT_DIRECTORY/event1" ]] || return 1
    [[ -f "$XORG_LOG" ]] || return 1
    grep -q 'FBDEV(0)' "$XORG_LOG" || return 1
    grep -q 'Adding extended input device.*Asterinas keyboard' "$XORG_LOG" || return 1
    grep -q 'Adding extended input device.*Asterinas pointer' "$XORG_LOG" || return 1
    pgrep -u "$USER_ID" -f '(^|/)matchbox-window-manager([[:space:]]|$)' \
        >/dev/null || return 1
    pgrep -u "$USER_ID" -x xterm >/dev/null || return 1
    window_tree="$(
        DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
            xwininfo -root -tree 2>/dev/null
    )" || return 1
    [[ "$window_tree" == *'"Asterinas Debian"'* ]] || return 1
}

while ! ready; do
    ((SECONDS < deadline)) || fail desktop-timeout
    sleep 1
done

emit "DEBIAN_DESKTOP_M3_UDEV state=active"
emit "DEBIAN_DESKTOP_M3_LOGIND state=active"
emit "DEBIAN_DESKTOP_M3_SESSION user=asterinas tty=tty1"
emit "DEBIAN_DESKTOP_M3_INPUT keyboard=evdev pointer=evdev"
emit "DEBIAN_DESKTOP_M3_XORG framebuffer=fbdev display=:0"
emit "DEBIAN_DESKTOP_M3_CLIENTS window-manager=matchbox terminal=xterm"
emit "DEBIAN_DESKTOP_M3_READY user=$USER_NAME display=:0"
