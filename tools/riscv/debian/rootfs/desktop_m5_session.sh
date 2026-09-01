#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"
readonly SESSION_LOG="$HOME/desktop-m5-session.log"

guest_monotonic_ns() {
    local raw="${EPOCHREALTIME-}"
    if [[ "$raw" =~ ^[0-9]+\.[0-9]{6}$ ]]; then
        raw="${raw/./}"
        printf '%s000' "$raw"
    else
        printf '%s000000000' "${EPOCHSECONDS:-0}"
    fi
}

emit_stage() {
    local stage="$1" pid="${2:-0}" detail="${3:-}" now line
    now="$(guest_monotonic_ns)"
    line="BROWSER_WEB_DESKTOP_STAGE=$stage guest_monotonic_ns=$now pid=$pid"
    [[ -z "$detail" ]] || line="$line $detail"
    printf '%s\n' "$line" >&2
    printf '%s\n' "$line" >>/dev/console 2>/dev/null || true
}

if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    # The online browser image does not need xinit's nested session, xterm, or
    # matchbox helper. Start Xorg directly and keep one lightweight WM so
    # Firefox gets a stable root window without the extra process chain.
    # Keep the marker on stderr as well as the console: systemd captures the
    # service stderr in the journal, while some Asterinas console devices do
    # not support direct writes from an unprivileged session process.
    emit_stage xorg-start
    # Mirror the online provider's Xorg logfile into the service stream.  Xorg
    # expects a regular logfile (using /dev/stderr makes it exit immediately
    # on this guest), while a failed gate may not persist the writable root.
    # The bounded tail keeps the decisive fbdev/VT error observable in the
    # journal+console stream without changing the Xorg invocation semantics.
    : >"$HOME/Xorg.0.log"
    # A failed Xorg can leave its Unix socket behind.  Never advertise that
    # stale endpoint as a live display to Firefox.
    /usr/bin/rm -f -- /tmp/.X11-unix/X0
    /usr/bin/tail -n 0 -f "$HOME/Xorg.0.log" >&2 &
    readonly xorg_log_tailer_pid=$!
    /usr/bin/Xorg :0 -noreset -nolisten tcp -ac -novtswitch -keeptty -extension GLX \
        -extension MIT-SHM -logfile "$HOME/Xorg.0.log" vt1 &
    readonly xorg_pid=$!
    for _ in {1..120}; do
        if [[ -S /tmp/.X11-unix/X0 ]] &&
            /usr/bin/timeout 5 /usr/bin/xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
            break
        fi
        if ! kill -0 "$xorg_pid" 2>/dev/null; then
            kill "$xorg_log_tailer_pid" 2>/dev/null || true
            emit_stage xorg-failed "$xorg_pid" reason=process-exited
            exit 1
        fi
        /usr/bin/sleep 1
    done
    if [[ ! -S /tmp/.X11-unix/X0 ]]; then
        kill "$xorg_log_tailer_pid" 2>/dev/null || true
        emit_stage xorg-failed "$xorg_pid" reason=socket-timeout
        exit 1
    fi
    emit_stage x-socket-ready "$xorg_pid"
    # Xorg owns the VT as the privileged display provider; keep the window
    # manager unprivileged so the desktop surface cannot grant Firefox extra
    # capabilities through the session process.
    /usr/bin/runuser --user asterinas --preserve-environment -- \
        /usr/bin/openbox --sm-disable >>"$SESSION_LOG" 2>&1 &
    wait "$xorg_pid"
    xorg_status=$?
    kill "$xorg_log_tailer_pid" 2>/dev/null || true
    wait "$xorg_log_tailer_pid" 2>/dev/null || true
    exit "$xorg_status"
fi

if [[ "${1-}" == --xsession ]]; then
    /usr/bin/matchbox-window-manager -use_titlebar yes &
    readonly window_manager_pid=$!
    /usr/bin/sleep 1
    /usr/bin/xterm -geometry 100x30+48+72 -title "Asterinas Browser M5" &
    wait "$window_manager_pid"
    exit $?
fi

exec /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
    -extension MIT-SHM -logfile "$HOME/Xorg.0.log" vt1 \
    >>"$SESSION_LOG" 2>&1
