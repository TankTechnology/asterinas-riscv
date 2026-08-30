#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_M4_CONSOLE:-/dev/console}"
readonly INPUT_DIRECTORY="${ASTERINAS_DESKTOP_M4_INPUT_DIRECTORY:-/dev/input}"
readonly XORG_LOG="${ASTERINAS_DESKTOP_M4_XORG_LOG:-/home/asterinas/Xorg.0.log}"
readonly SESSION_LOG="${ASTERINAS_DESKTOP_M4_SESSION_LOG:-/home/asterinas/desktop-m4-session.log}"
readonly NETSURF_LOG="${ASTERINAS_DESKTOP_M4_NETSURF_LOG:-/home/asterinas/netsurf-m7.log}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M4_TIMEOUT_SECONDS:-240}"
readonly PROBE_TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M4_PROBE_TIMEOUT_SECONDS:-30}"
readonly USER_NAME="asterinas"
readonly USER_ID="1000"
not_ready_reason="not-evaluated"

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

bounded() {
    timeout --signal=KILL "${PROBE_TIMEOUT_SECONDS}s" "$@"
}

record_probe_failure() {
    local reason="$1"
    local status="$2"

    if ((status == 124 || status == 137)); then
        not_ready_reason="$reason-probe-timeout"
    else
        not_ready_reason="$reason"
    fi
}

probe() {
    local reason="$1"
    local status
    shift

    if bounded "$@"; then
        return 0
    else
        status=$?
    fi
    record_probe_failure "$reason" "$status"
    return 1
}

fail() {
    local reason="$1"
    bounded systemctl --no-pager --full status \
        dbus.service \
        systemd-logind.service \
        asterinas-desktop-m4.service \
        >>"$CONSOLE" 2>&1 || true
    if [[ -f "$SESSION_LOG" ]]; then
        printf '%s\n' '--- desktop session log ---' >>"$CONSOLE"
        cat -- "$SESSION_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    if [[ -f "$NETSURF_LOG" && ! -L "$NETSURF_LOG" ]]; then
        printf '%s\n' '--- NetSurf log tail ---' >>"$CONSOLE"
        tail -c 8192 -- "$NETSURF_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    printf '%s\n' '--- X window tree tail ---' >>"$CONSOLE"
    bounded env DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
        xwininfo -root -tree 2>&1 | tail -c 16384 >>"$CONSOLE" || true
    printf '\n' >>"$CONSOLE"
    if [[ -f "$XORG_LOG" ]]; then
        printf '%s\n' '--- Xorg log ---' >>"$CONSOLE"
        cat -- "$XORG_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    emit "DEBIAN_DESKTOP_M4_FAIL reason=$reason"
    exit 1
}

move_single_window_to_overview_workspace() {
    local instance_name="$1"
    local failure_prefix="$2"
    local window_output
    local -a windows=()

    if ! window_output="$(
        DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
            xdotool search --onlyvisible --classname "^${instance_name}$"
    )"; then
        fail "$failure_prefix-search"
    fi
    ((${#window_output} <= 128)) || fail "$failure_prefix-search-output-too-long"
    mapfile -t windows <<<"$window_output"
    ((${#windows[@]} == 1)) || fail "$failure_prefix-window-count"
    [[ "${windows[0]}" =~ ^[1-9][0-9]*$ ]] || \
        fail "$failure_prefix-window-id"
    DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
        xdotool set_desktop_for_window "${windows[0]}" 1 || \
        fail "$failure_prefix-workspace"
}

[[ "$PROBE_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-probe-timeout
[[ "$TIMEOUT_SECONDS" =~ ^(0|[1-9][0-9]*)$ ]] || fail invalid-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))

ready() {
    local sessions
    local status
    local window_tree
    local window_tree_lower

    not_ready_reason="not-evaluated"
    probe udev systemctl is-active --quiet systemd-udevd.service || return 1
    probe logind systemctl is-active --quiet systemd-logind.service || return 1
    sessions="$(bounded loginctl list-sessions --no-legend 2>/dev/null)"
    status=$?
    if ((status != 0)); then
        record_probe_failure login-sessions "$status"
        return 1
    fi
    [[ "$sessions" =~ (^|[[:space:]])$USER_NAME([[:space:]]|$) ]] || {
        not_ready_reason="asterinas-session"
        return 1
    }
    [[ -e "$INPUT_DIRECTORY/event0" ]] || {
        not_ready_reason="keyboard-device"
        return 1
    }
    [[ -e "$INPUT_DIRECTORY/event1" ]] || {
        not_ready_reason="pointer-device"
        return 1
    }
    [[ -f "$XORG_LOG" ]] || {
        not_ready_reason="xorg-log"
        return 1
    }
    probe framebuffer grep -q 'FBDEV(0)' "$XORG_LOG" || return 1
    probe keyboard-xinput grep -q \
        'Adding extended input device.*Asterinas keyboard' "$XORG_LOG" || return 1
    probe pointer-xinput grep -q \
        'Adding extended input device.*Asterinas pointer' "$XORG_LOG" || return 1
    probe openbox pgrep -u "$USER_ID" -x openbox >/dev/null || return 1
    probe pcmanfm pgrep -u "$USER_ID" -f \
        '(^|/)pcmanfm([[:space:]].*)?--desktop([[:space:]]|$)' \
        >/dev/null || return 1
    probe lxpanel pgrep -u "$USER_ID" -f \
        '(^|/)lxpanel([[:space:]]|$)' >/dev/null || return 1
    probe netsurf pgrep -u "$USER_ID" -x netsurf-gtk >/dev/null || return 1
    probe xterm pgrep -u "$USER_ID" -x xterm >/dev/null || return 1
    probe netsurf-window env DISPLAY=:0 \
        XAUTHORITY=/home/asterinas/.Xauthority \
        xdotool search --onlyvisible --classname '^netsurf-gtk$' \
        >/dev/null || return 1
    probe xterm-window env DISPLAY=:0 \
        XAUTHORITY=/home/asterinas/.Xauthority \
        xdotool search --onlyvisible --classname '^xterm$' \
        >/dev/null || return 1
    window_tree="$(
        bounded env DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority \
            xwininfo -root -tree 2>/dev/null
    )"
    status=$?
    if ((status != 0)); then
        record_probe_failure window-tree "$status"
        return 1
    fi
    window_tree_lower="${window_tree,,}"
    [[ "$window_tree_lower" == *netsurf* ]] || {
        not_ready_reason="netsurf-tree"
        return 1
    }
    [[ "$window_tree_lower" == *"asterinas terminal"* ]] || {
        not_ready_reason="terminal-tree"
        return 1
    }
}

while ! ready; do
    if ((SECONDS >= deadline)); then
        emit "DEBIAN_DESKTOP_M4_DIAGNOSTIC missing=$not_ready_reason"
        fail desktop-timeout
    fi
    sleep 1
done

if [[ "${ASTERINAS_DESKTOP_SHOW_OVERVIEW:-0}" == 1 ]]; then
    move_single_window_to_overview_workspace netsurf-gtk overview-browser
    move_single_window_to_overview_workspace xterm overview-terminal
    sleep 1
fi

emit "DEBIAN_DESKTOP_M4_UDEV state=active"
emit "DEBIAN_DESKTOP_M4_LOGIND state=active"
emit "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1"
emit "DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev"
emit "DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0"
emit "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3"
emit "DEBIAN_DESKTOP_M4_CLIENTS window-manager=openbox file-manager=pcmanfm browser=netsurf terminal=xterm"
emit "DEBIAN_DESKTOP_M4_READY user=$USER_NAME display=:0"
