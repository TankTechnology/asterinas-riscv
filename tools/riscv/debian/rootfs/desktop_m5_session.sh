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
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=xorg-start' >>/dev/console
    /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
        -extension MIT-SHM -logfile "$HOME/Xorg.0.log" vt1 \
        >>"$SESSION_LOG" 2>&1 &
    readonly xorg_pid=$!
    for _ in {1..120}; do
        [[ -S /tmp/.X11-unix/X0 ]] && break
        kill -0 "$xorg_pid" 2>/dev/null || exit 1
        /usr/bin/sleep 1
    done
    [[ -S /tmp/.X11-unix/X0 ]] || exit 1
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=x-socket-ready' >>/dev/console
    /usr/bin/openbox --sm-disable >>"$SESSION_LOG" 2>&1 &
    wait "$xorg_pid"
    exit $?
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
