#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Regression for the DRM fake-card0 hang: the scenario that hung glxgears for
# >9 minutes (Xorg started with softpipe, glxgears taking Mesa's default
# llvmpipe driver, fake card0) repeated ten times.  With the bounded KMS event
# and fence waits, every run must finish within the timeout.
#
# Markers: drm_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
LOG=/tmp/drm-hang.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now_ms() { date +%s%N | cut -c1-13; }

log "drm_regress_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [ ! -S /tmp/.X11-unix/X0 ]; then
    GALLIUM_DRIVER=softpipe LIBGL_ALWAYS_SOFTWARE=1 \
        /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/drm-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
fi
log "drm_xorg up=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"

# glxinfo first (bounded), then ten glxgears runs.
gx=$(timeout 15 glxinfo -B 2>&1); gxrc=$?
log "drm_glxinfo rc=$gxrc renderer=$(printf '%s' "$gx" | grep -E 'OpenGL renderer' | head -c 160)"

run=0
hangs=0
while [ "$run" -lt 10 ]; do
    run=$((run + 1))
    t0=$(now_ms)
    gg=$(timeout 20 glxgears -info 2>&1); rc=$?
    t1=$(now_ms)
    dur=$((t1 - t0))
    fps=$(printf '%s' "$gg" | grep -oE '[0-9.]+ FPS' | tail -1)
    [ "$rc" -eq 124 ] && hangs=$((hangs + 1))
    log "drm_glx run=$run rc=$rc dur_ms=$dur fps=${fps:-none}"
done
log "drm_glx_summary runs=10 hangs=$hangs"

log "drm_regress_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
