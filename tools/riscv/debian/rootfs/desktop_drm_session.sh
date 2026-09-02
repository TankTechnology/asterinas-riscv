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

# GLX stays enabled: on the virgl device glamor provides real 3D, and on the
# 2D device AIGLX still offers llvmpipe; only MIT-SHM is disabled.
# Diagnostic images carry the M19 ioctl logger at /usr/lib/asterinas/ so the
# X server's own DRM ioctls land in the session log.
xorg_env=()
if [[ -f /usr/lib/asterinas/ioctltrace.so ]]; then
    xorg_env+=(LD_PRELOAD=/usr/lib/asterinas/ioctltrace.so)
fi
exec env "${xorg_env[@]}" /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp \
    -extension MIT-SHM -logfile "$HOME/Xorg.0.log" vt1 \
    >>"$SESSION_LOG" 2>&1
