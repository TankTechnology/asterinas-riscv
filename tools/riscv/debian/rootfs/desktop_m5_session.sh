#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"
readonly SESSION_LOG="$HOME/desktop-m5-session.log"

if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    # The online browser image does not need xinit's nested session, xterm, or
    # matchbox helper. Start Xorg directly and keep one lightweight WM so
    # Firefox gets a stable root window without the extra process chain.
    # Keep the marker on stderr as well as the console: systemd captures the
    # service stderr in the journal, while some Asterinas console devices do
    # not support direct writes from an unprivileged session process.
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=xorg-start' >&2
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=xorg-start' >>/dev/console 2>/dev/null || true
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
            printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=xorg-failed reason=process-exited' >&2
            exit 1
        fi
        /usr/bin/sleep 1
    done
    if [[ ! -S /tmp/.X11-unix/X0 ]]; then
        kill "$xorg_log_tailer_pid" 2>/dev/null || true
        printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=xorg-failed reason=socket-timeout' >&2
        exit 1
    fi
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=x-socket-ready' >&2
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=x-socket-ready' >>/dev/console 2>/dev/null || true
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
