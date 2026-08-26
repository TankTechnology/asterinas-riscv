#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"
readonly SESSION_LOG="$HOME/desktop-m4-session.log"

if [[ "${1-}" == --xsession ]]; then
    /usr/bin/matchbox-window-manager -use_titlebar yes &
    readonly window_manager_pid=$!
    /usr/bin/sleep 1
    /usr/bin/pcmanfm --no-desktop "/home/asterinas/Asterinas Files" &
    /usr/bin/netsurf-gtk file:///usr/share/asterinas/desktop-m4-welcome.html &
    /usr/bin/sleep 1
    /usr/bin/xterm -geometry 100x30+48+72 -title "Asterinas Terminal" &
    wait "$window_manager_pid"
    exit $?
fi

{
    printf 'DESKTOP_M4_DEVICE identity='
    /usr/bin/id
    shopt -s nullglob
    readonly input_devices=(/dev/input/event*)
    /usr/bin/stat -c 'DESKTOP_M4_DEVICE path=%n uid=%u gid=%g mode=%a type=%F' \
        /dev/fb0 "${input_devices[@]}"
    if exec 9<>/dev/fb0; then
        printf '%s\n' 'DESKTOP_M4_DEVICE framebuffer-open-rw=ok'
        exec 9>&-
    else
        printf '%s\n' 'DESKTOP_M4_DEVICE framebuffer-open-rw=failed'
    fi
} >>"$SESSION_LOG" 2>&1

exec /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
    -extension MIT-SHM \
    -logfile "$HOME/Xorg.0.log" vt1 \
    >>"$SESSION_LOG" 2>&1
