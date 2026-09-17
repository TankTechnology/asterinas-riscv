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

# Record the acceleration setup in the serial transcript so that gate runs
# are self-diagnosing (e.g. glamor falling back to software rendering).
grep -E 'glamor|AIGLX|DRI3|Modeline' "$XORG_LOG" >>"$CONSOLE" 2>&1 || true

# Report the GL renderer so gates can prove virgl acceleration instead of
# inferring it from the boot device.  Under TCG a single virgl glxinfo run
# can take tens of seconds (every Gallium step is an emulated round-trip to
# the host GPU), so each attempt gets a generous budget.  Every step is
# failure-tolerant: a failing probe must not kill the evidence run (the
# script uses `set -e`) before the renderer marker is emitted.
#
# /usr/lib/asterinas/ioctltrace.so (the M19 LD_PRELOAD ioctl logger) is
# injected into diagnostic images to name the exact DRM ioctl sequence.
glxinfo_env=(DISPLAY=:0 XAUTHORITY="/home/$USER_NAME/.Xauthority")
if [[ -f /usr/lib/asterinas/ioctltrace.so ]]; then
    glxinfo_env+=(LD_PRELOAD=/usr/lib/asterinas/ioctltrace.so)
fi

gl_renderer="unavailable"
gl_diag_dumped=""
for _ in $(seq 1 4); do
    probe="$(env "${glxinfo_env[@]}" timeout 60 glxinfo -B 2>/dev/null | \
        sed -n 's/^OpenGL renderer string: //p' | head -1 || true)"
    if [[ -n "$probe" ]]; then
        gl_renderer="$probe"
        break
    fi
    if [[ -z "$gl_diag_dumped" ]]; then
        gl_diag_dumped=yes
        emit '--- DRM GL probe diagnostics ---'
        # Run one probed glxinfo in the background and sample where it is
        # stuck so a hang names the blocking kernel wait channel.
        env "${glxinfo_env[@]}" glxinfo -B >>"$CONSOLE" 2>&1 &
        gl_pid=$!
        gl_waited=0
        while kill -0 "$gl_pid" 2>/dev/null; do
            if (( gl_waited >= 90 )); then
                emit "--- glxinfo[$gl_pid] stuck: $(cat "/proc/$gl_pid/wchan" 2>/dev/null) ---"
                grep -E '^(State|Name|Pid|PPid)' "/proc/$gl_pid/status" >>"$CONSOLE" 2>&1 || true
                cat "/proc/$gl_pid/stack" >>"$CONSOLE" 2>&1 || true
                kill -9 "$gl_pid" 2>/dev/null || true
                break
            fi
            sleep 5
            gl_waited=$((gl_waited + 5))
        done
        wait "$gl_pid" 2>/dev/null || true
        if [[ -f "$SESSION_LOG" ]]; then
            emit '--- DRM GL probe: session log tail ---'
            tail -c 8192 "$SESSION_LOG" >>"$CONSOLE" 2>&1 || true
        fi
        if [[ -f "$XORG_LOG" ]]; then
            emit '--- DRM GL probe: Xorg log tail ---'
            tail -c 8192 "$XORG_LOG" >>"$CONSOLE" 2>&1 || true
        fi
    fi
    sleep 5
done
emit "DEBIAN_DESKTOP_DRM_GL renderer=$gl_renderer"
