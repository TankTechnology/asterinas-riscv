#!/bin/sh
# One-shot DRM hang probe with kernel-side wait logging.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
log() { printf '%s\n' "$*" >/dev/console; }
log "once_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if [ ! -S /tmp/.X11-unix/X0 ]; then
    GALLIUM_DRIVER=softpipe LIBGL_ALWAYS_SOFTWARE=1 \
        /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/once-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
fi
log "once_xorg up=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"
timeout 20 glxgears -info >/tmp/once-glx.log 2>&1
log "once_glx rc=$? out=$(tail -2 /tmp/once-glx.log | tr '\n' '|' | head -c 200)"
log "once_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
