#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Second Firefox probe: the same measurements without forcing softpipe, so
# Mesa's default choice (llvmpipe) is exercised.  glxinfo/glxgears/Firefox are
# all wrapped in timeouts: llvmpipe talks to the fake card0 and may block on
# an unimplemented DRM ioctl.
#
# Markers: lp_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
export MOZ_CRASHREPORTER_DISABLE=1
export MOZ_DISABLE_CONTENT_SANDBOX=1

LOG=/tmp/firefox-llvmpipe.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now_ms() { date +%s%N | cut -c1-13; }

log "lp_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log "lp_render_env GALLIUM_DRIVER=${GALLIUM_DRIVER:-unset} LIBGL_ALWAYS_SOFTWARE=${LIBGL_ALWAYS_SOFTWARE:-unset}"

if [ ! -S /tmp/.X11-unix/X0 ]; then
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/lp-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
fi
log "lp_xorg up=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no) dri=$(ls /dev/dri 2>&1 | tr '\n' ' ') fb=$(ls /dev/fb* 2>&1 | tr '\n' ' ')"

gx=$(timeout 20 glxinfo -B 2>&1); gxrc=$?
log "lp_glxinfo rc=$gxrc renderer=$(printf '%s' "$gx" | grep -E 'OpenGL renderer|OpenGL version' | tr '\n' '|' | head -c 240)"

gg=$(timeout 20 glxgears -info 2>&1 | tail -15); ggrc=$?
log "lp_glxgears rc=$ggrc out=$(printf '%s' "$gg" | grep -E 'GL_RENDERER|frames in' | tr '\n' '|' | head -c 300)"

python3 - <<'PY'
lines = "".join("<p>line %04d: the quick brown fox jumps over the lazy dog</p>\n" % i for i in range(1, 401))
open("/tmp/lp-page.html", "w").write("<!doctype html><meta charset=utf-8><title>lag</title><body>%s</body>" % lines)
PY

ff_pids() {
    for p in /proc/[0-9]*; do
        [ -r "$p/comm" ] || continue
        case "$(cat "$p/comm" 2>/dev/null)" in
            firefox*|"Web Content"*|RDD*) basename "$p" ;;
        esac
    done
}
cpu_times() {
    ut=0; st=0
    for p in $(ff_pids); do
        set -- $(sed 's/^.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $12, $13}')
        [ -n "${1:-}" ] && ut=$((ut + $1)) && st=$((st + $2))
    done
    echo "$ut $st"
}

rm -rf /tmp/lpprof && mkdir -p /tmp/lpprof
firefox-esr --no-remote --new-instance --profile /tmp/lpprof /tmp/lp-page.html \
    >/tmp/lp-ff.log 2>&1 &
ff_launch=$!
t0=$(now_ms)
win=""
i=0
while [ "$i" -lt 120 ]; do
    win=$(timeout 1 xdotool search --class firefox 2>/dev/null | tail -1)
    [ -n "$win" ] && break
    sleep 0.5
    i=$((i + 1))
done
t1=$(now_ms)
if [ -z "$win" ]; then
    log "lp_ff startup=fail launch_ms=$((t1 - t0)) log=$(head -3 /tmp/lp-ff.log | tr '\n' '|' | head -c 300)"
else
    log "lp_ff startup_ok launch_ms=$((t1 - t0)) win=$win"
    timeout 3 xdotool windowactivate "$win" 2>/dev/null
    sleep 2
    set -- $(cpu_times); ut0=$1; st0=$2
    t2=$(now_ms)
    i=0
    while [ "$i" -lt 20 ]; do xdotool key Page_Down 2>/dev/null; i=$((i+1)); done
    t3=$(now_ms)
    sleep 1
    set -- $(cpu_times); ut1=$1; st1=$2
    log "lp_ff scroll20_ms=$((t3 - t2)) user_ticks=$((ut1 - ut0)) sys_ticks=$((st1 - st0))"
fi
kill "$ff_launch" 2>/dev/null
pkill -f firefox-esr 2>/dev/null
pkill -f 'Web Content' 2>/dev/null

log "lp_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
