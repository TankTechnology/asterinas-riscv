#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"
readonly SESSION_LOG="$HOME/desktop-m5-session.log"
readonly PROBE_URL="file:///usr/share/asterinas/browser-m5/index.html"
readonly FIREFOX_PROFILE="$HOME/.mozilla/asterinas-browser-m5"

if [[ "${1-}" == --xsession ]]; then
    /usr/bin/matchbox-window-manager -use_titlebar yes &
    readonly window_manager_pid=$!
    /usr/bin/sleep 1
    /usr/bin/mkdir -p -- "$FIREFOX_PROFILE"
    /usr/bin/firefox-esr --no-remote --new-instance --offline \
        --profile "$FIREFOX_PROFILE" "$PROBE_URL" &
    /usr/bin/sleep 1
    /usr/bin/xterm -geometry 100x30+48+72 -title "Asterinas Browser M5" &
    wait "$window_manager_pid"
    exit $?
fi

exec /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
    -extension MIT-SHM -logfile "$HOME/Xorg.0.log" vt1 \
    >>"$SESSION_LOG" 2>&1
