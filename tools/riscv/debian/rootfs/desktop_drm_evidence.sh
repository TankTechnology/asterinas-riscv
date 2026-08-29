#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_DRM_CONSOLE:-/dev/console}"
readonly XORG_LOG="${ASTERINAS_DESKTOP_DRM_XORG_LOG:-/home/asterinas/Xorg.0.log}"
readonly SESSION_LOG="${ASTERINAS_DESKTOP_DRM_SESSION_LOG:-/home/asterinas/desktop-drm-session.log}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_DRM_TIMEOUT_SECONDS:-300}"
readonly USER_NAME=asterinas
readonly USER_ID=1000

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }
fail() {
    if [[ -f "$SESSION_LOG" ]]; then
        printf '%s\n' '--- DRM desktop session log ---' >>"$CONSOLE"
        tail -c 16384 "$SESSION_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    if [[ -f "$XORG_LOG" ]]; then
        printf '%s\n' '--- DRM Xorg log ---' >>"$CONSOLE"
        tail -c 16384 "$XORG_LOG" >>"$CONSOLE" 2>&1 || true
    fi
    emit "DEBIAN_DESKTOP_DRM_FAIL reason=$1"
    exit 1
}

[[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || fail invalid-timeout
deadline=$((SECONDS + TIMEOUT_SECONDS))
ready() {
    systemctl is-active --quiet systemd-udevd.service || return 1
    systemctl is-active --quiet systemd-logind.service || return 1
    loginctl list-sessions --no-legend 2>/dev/null | grep -q " $USER_NAME " || return 1
    [[ -c /dev/dri/card0 && -e /dev/input/event0 && -e /dev/input/event1 ]] || return 1
    [[ -f "$XORG_LOG" ]] || return 1
    grep -q 'modesetting_drv.so' "$XORG_LOG" || return 1
    grep -Eq 'drm|DRI3|virtio' "$XORG_LOG" || return 1
    pgrep -u "$USER_ID" -x openbox >/dev/null || return 1
    pgrep -u "$USER_ID" -f 'pcmanfm.*--desktop' >/dev/null || return 1
    pgrep -u "$USER_ID" -x lxpanel >/dev/null || return 1
    pgrep -u "$USER_ID" -x xterm >/dev/null || return 1
}

while ! ready; do
    ((SECONDS < deadline)) || fail desktop-timeout
    sleep 1
done

emit "DEBIAN_DESKTOP_DRM_UDEV state=active"
emit "DEBIAN_DESKTOP_DRM_LOGIND state=active"
emit "DEBIAN_DESKTOP_DRM_SESSION user=$USER_NAME tty=tty1"
emit "DEBIAN_DESKTOP_DRM_INPUT keyboard=evdev pointer=evdev"
emit 'DEBIAN_DESKTOP_DRM_XORG driver=modesetting device=virtio-gpu drm=active display=:0'
emit 'DEBIAN_DESKTOP_DRM_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm'
emit "DEBIAN_DESKTOP_DRM_READY user=$USER_NAME display=:0"
