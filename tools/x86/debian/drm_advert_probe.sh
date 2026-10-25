#!/bin/sh
# Probe Mesa's DRI driver selection on the fake card (hang environment).
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
log() { printf '%s\n' "$*" >/dev/console; }
log "adv_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if [ ! -S /tmp/.X11-unix/X0 ]; then
    GALLIUM_DRIVER=softpipe LIBGL_ALWAYS_SOFTWARE=1 \
        /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/adv-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
fi
log "adv_xorg up=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"
gx=$(timeout 20 glxinfo -B 2>&1); rc=$?
log "adv_glxinfo rc=$rc out=$(printf '%s' "$gx" | grep -E 'OpenGL renderer|failed|error' | tr '\n' '|' | head -c 300)"
gg=$(timeout 20 glxgears -info 2>&1); rc=$?
log "adv_glxgears rc=$rc out=$(printf '%s' "$gg" | grep -E 'GL_RENDERER|frames in|failed|error' | tail -3 | tr '\n' '|' | head -c 300)"
log "adv_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
