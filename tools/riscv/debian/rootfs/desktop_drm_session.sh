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
# ioctltrace is the only shim carried, and it has never produced a line.
if [[ -f /usr/lib/asterinas/ioctltrace.so ]]; then
    xorg_env+=(LD_PRELOAD=/usr/lib/asterinas/ioctltrace.so)
fi

# Mesa/EGL debugging on the X server itself, off unless asked for
# (`asterinas.egl_probe=1`).
#
# This has to be the X server's environment, not a probe's: the decision that
# matters is the one Xorg makes when glamor asks for an EGL device, and a
# separate probe reaching the DRM node on its own terms can fail somewhere
# else entirely and report a failure the desktop never took.  Xorg's stderr
# already goes to the session log, which the evidence script now reads.
if tr ' ' '\n' </proc/cmdline 2>/dev/null | grep -qx 'asterinas.egl_probe=1'; then
    xorg_env+=(EGL_LOG_LEVEL=debug LIBGL_DEBUG=verbose MESA_DEBUG=1)
fi

# LD_DEBUG is the loader's own tracing, built into glibc.  An LD_PRELOAD shim
# meant to answer the same question crashed the X server instead -- interposing
# dlsym breaks glibc's internal symbol resolution -- and this needs no shim at
# all: it names every file the loader searched for and what it did when it
# could not find one.
#
# It rides its own flag rather than `egl_probe`, because it is enormously
# expensive under emulation -- tracing every library the X server loads turned
# a ten-second start into five minutes -- and once the loader question is
# answered it is pure cost.  The EGL variables above stay cheap.
if tr ' ' '\n' </proc/cmdline 2>/dev/null | grep -qx 'asterinas.loader_trace=1'; then
    xorg_env+=(LD_DEBUG=libs)
fi
exec env "${xorg_env[@]}" /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp \
    -extension MIT-SHM -logfile "$HOME/Xorg.0.log" vt1 \
    >>"$SESSION_LOG" 2>&1
