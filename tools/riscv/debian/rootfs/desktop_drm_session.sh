#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"
readonly SESSION_LOG="$HOME/desktop-drm-session.log"

if [[ "${1-}" == --xsession ]]; then
    /usr/bin/openbox &
    readonly window_manager_pid=$!
    /usr/bin/sleep 1
    /usr/bin/pcmanfm --desktop --profile Asterinas &
    /usr/bin/lxpanel --profile Asterinas &
    /usr/bin/xterm -geometry 100x30+48+72 -title "Asterinas DRM Terminal" &
    wait "$window_manager_pid"
    exit $?
fi

{
    printf 'DESKTOP_DRM_DEVICE identity='
    /usr/bin/id
    shopt -s nullglob
    readonly input_devices=(/dev/input/event*)
    /usr/bin/stat -c 'DESKTOP_DRM_DEVICE path=%n uid=%u gid=%g mode=%a type=%F' \
        /dev/dri/card0 /dev/dri/renderD* "${input_devices[@]}"
    if exec 9<>/dev/dri/card0; then
        printf '%s\n' 'DESKTOP_DRM_DEVICE card-open-rw=ok'
        exec 9>&-
    else
        printf '%s\n' 'DESKTOP_DRM_DEVICE card-open-rw=failed'
    fi
} >>"$SESSION_LOG" 2>&1

exec /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp -extension GLX \
    -extension MIT-SHM -logfile "$HOME/Xorg.0.log" vt1 \
    >>"$SESSION_LOG" 2>&1
