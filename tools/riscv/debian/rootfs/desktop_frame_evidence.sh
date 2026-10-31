#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Observe the finished graphical desktop and require a real Firefox window.
#
# This runs as a boot-time systemd observer, not as part of the session.  It
# waits for Xorg, lets the desktop settle, then looks for a visible Firefox
# window with xdotool.  The host gate reads the RAM-backed guest framebuffer
# through the QEMU monitor, so this observer only has to prove that the window
# exists; it deliberately does not depend on xwd or a second framebuffer copy.

set -u

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"

readonly CONSOLE="${ASTERINAS_DESKTOP_CONSOLE:-/dev/console}"
readonly WINDOW_TIMEOUT="${ASTERINAS_DESKTOP_FIREFOX_TIMEOUT:-480}"
readonly X_TIMEOUT="${ASTERINAS_DESKTOP_X_TIMEOUT:-180}"
readonly DESKTOP_SETTLE_SECONDS="${ASTERINAS_DESKTOP_SETTLE_SECONDS:-60}"
readonly POLL_SECONDS="${ASTERINAS_DESKTOP_POLL_SECONDS:-2}"
readonly TIMELINE="$HOME/browser-web-timeline.log"
readonly FIREFOX_STDERR="$HOME/firefox-web-stderr.log"

emit() {
    local line="$1"
    printf '%s\n' "$line" >&2
    printf '%s\n' "$line" >>"$CONSOLE" 2>/dev/null || true
}

sanitize() {
    local value="$1"
    value="${value//$'\n'/ }"
    value="${value//$'\r'/ }"
    value="${value//[[:space:]]/_}"
    value="${value//[^A-Za-z0-9_.:+-]/}"
    printf '%s' "${value:0:96}"
}

firefox_process_count() {
    local total=0 count name
    for name in firefox firefox-esr; do
        count="$(timeout 5 /usr/bin/pgrep -c -x "$name" 2>/dev/null || true)"
        total=$((total + ${count:-0}))
    done
    printf '%s' "$total"
}

diagnose() {
    local state count line
    state="$(timeout 5 /usr/bin/systemctl is-active asterinas-browser-web.service 2>/dev/null || true)"
    count="$(firefox_process_count)"
    line="BROWSER_WEB_DESKTOP_FIREFOX_DIAG service=$(sanitize "${state:-unknown}")"
    line+=" firefox_processes=$(sanitize "${count:-0}")"
    emit "$line"
    if [[ -r "$TIMELINE" ]]; then
        timeout 5 /usr/bin/tail -n 8 "$TIMELINE" 2>/dev/null |
            while IFS= read -r line; do
                emit "BROWSER_WEB_DESKTOP_FIREFOX_TIMELINE $(sanitize "$line")"
            done
    fi
    if [[ -s "$FIREFOX_STDERR" ]]; then
        timeout 5 /usr/bin/tail -n 5 "$FIREFOX_STDERR" 2>/dev/null |
            while IFS= read -r line; do
                emit "BROWSER_WEB_DESKTOP_FIREFOX_STDERR $(sanitize "$line")"
            done
    fi
}

wait_for_x() {
    local deadline=$((SECONDS + X_TIMEOUT))
    while ((SECONDS < deadline)); do
        if [[ -S /tmp/.X11-unix/X0 ]] &&
            timeout 5 /usr/bin/xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
            return 0
        fi
        /usr/bin/sleep 1
    done
    return 1
}

window_geometry_is_usable() {
    local id="$1" geometry width height
    geometry="$(timeout 5 /usr/bin/xdotool getwindowgeometry --shell "$id" 2>/dev/null || true)"
    width="$(/usr/bin/awk -F= '$1 == "WIDTH" { print $2; exit }' <<<"$geometry")"
    height="$(/usr/bin/awk -F= '$1 == "HEIGHT" { print $2; exit }' <<<"$geometry")"
    [[ "$width" =~ ^[0-9]+$ && "$height" =~ ^[0-9]+$ ]] || return 1
    ((width >= 400 && height >= 300))
}

window_from_xwininfo() {
    local line id title
    line="$(timeout 5 /usr/bin/xwininfo -root -tree 2>/dev/null |
        /usr/bin/grep -iE 'Mozilla Firefox|"Navigator"|"Firefox"' |
        /usr/bin/head -n 1 || true)"
    [[ -n "$line" ]] || return 1
    id="$(/usr/bin/awk '{print $1}' <<<"$line")"
    title="$(/usr/bin/sed -E 's/^[^"]*"([^"]*)".*/\1/' <<<"$line")"
    [[ "$id" =~ ^0x[0-9a-fA-F]+$ ]] || return 1
    window_geometry_is_usable "$id" || return 1
    printf '%s %s\n' "$id" "$title"
}

find_firefox_window() {
    local deadline=$((SECONDS + WINDOW_TIMEOUT)) ids id title window next_report=$((SECONDS + 30))
    local service_state processes wrapper_pid line
    while ((SECONDS < deadline)); do
        if ((SECONDS >= next_report)); then
            service_state="$(timeout 5 /usr/bin/systemctl is-active asterinas-browser-web.service 2>/dev/null || true)"
            processes="$(firefox_process_count)"
            wrapper_pid="$(timeout 5 /usr/bin/cat "$HOME/browser-web-firefox.pid" 2>/dev/null || true)"
            line="BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=waiting"
            line+=" elapsed=$((SECONDS - deadline + WINDOW_TIMEOUT))"
            line+=" service=$(sanitize "${service_state:-unknown}")"
            line+=" processes=$(sanitize "${processes:-0}")"
            line+=" wrapper_pid=$(sanitize "${wrapper_pid:-none}")"
            emit "$line"
            next_report=$((SECONDS + 30))
        fi
        ids="$(timeout 5 /usr/bin/xdotool search --onlyvisible --class '[Ff]irefox' 2>/dev/null || true)"
        if [[ -z "$ids" ]]; then
            ids="$(timeout 5 /usr/bin/xdotool search --onlyvisible --name 'Mozilla Firefox' 2>/dev/null || true)"
        fi
        for id in $ids; do
            title="$(timeout 5 /usr/bin/xdotool getwindowname "$id" 2>/dev/null || true)"
            if [[ -n "$title" ]] && window_geometry_is_usable "$id"; then
                printf '%s %s\n' "$id" "$title"
                return 0
            fi
        done
        if window="$(window_from_xwininfo)"; then
            printf '%s\n' "$window"
            return 0
        fi
        /usr/bin/sleep "$POLL_SECONDS"
    done
    return 1
}

emit "BROWSER_WEB_DESKTOP_FRAME_STAGE=service-start pid=$$"

if ! wait_for_x; then
    emit "BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=timeout reason=x-server"
    diagnose
    exit 1
fi
emit "BROWSER_WEB_DESKTOP_FRAME_STAGE=x-ready"

/usr/bin/sleep "$DESKTOP_SETTLE_SECONDS"
emit "BROWSER_WEB_DESKTOP_FRAME_STAGE=desktop"

if ! read -r window_id window_title < <(find_firefox_window); then
    emit "BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=timeout reason=no-visible-window"
    diagnose
    exit 1
fi

line="BROWSER_WEB_DESKTOP_FIREFOX_WINDOW=ready id=$(sanitize "$window_id")"
line+=" title=$(sanitize "$window_title")"
emit "$line"

# The window is mapped before its first content paint on this CPU.  A bounded
# delay gives the renderer time to paint the page; the host reads the actual
# pixels from guest memory after this marker.
/usr/bin/sleep 5
emit "BROWSER_WEB_DESKTOP_FRAME_READY source=host-pmemsave"
exit 0
