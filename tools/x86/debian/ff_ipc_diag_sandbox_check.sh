#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Launch Firefox and report how many of its processes survive (content
# processes), for diagnosing the IPC EPERM that kills them.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
export MOZ_CRASHREPORTER_DISABLE=1

log() { printf '%s\n' "$*" | tee -a /tmp/ff_ipc.log >/dev/console; }

log "ffipc_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if [ ! -S /tmp/.X11-unix/X0 ]; then
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-noudev.conf >/tmp/ffipc-xorg.log 2>&1 &
    i=0; while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
    command -v openbox >/dev/null 2>&1 && { openbox >/tmp/ffipc-openbox.log 2>&1 & sleep 1; }
fi
python3 - <<'PY'
lines = "".join("<p>line %04d: the quick brown fox jumps over the lazy dog</p>\n" % i for i in range(1, 2001))
open("/tmp/ffipc-page.html", "w").write("<!doctype html><meta charset=utf-8><title>ipc</title><body>%s</body>" % lines)
PY
rm -rf /tmp/ffipc-prof && mkdir -p /tmp/ffipc-prof
firefox-esr --no-remote --new-instance --profile /tmp/ffipc-prof /tmp/ffipc-page.html >/tmp/ffipc-ff.log 2>&1 &
ff=$!
sleep 25
n=$(ls /proc | grep -cE '^[0-9]+$')
procs=""
count=0
for p in /proc/[0-9]*; do
    pid=${p#/proc/}
    comm=$(cat "$p/comm" 2>/dev/null) || continue
    case "$comm" in
        firefox*|Web*|Isolated*|RDD*|Socket*|Privileged*|Utility*|Forkserver*)
            count=$((count+1)); procs="$procs ${pid}:$(printf '%s' "$comm" | tr ' ' '_')"
            ;;
    esac
done
log "ffipc_procs count=$count list=$procs"
log "ffipc_fflog $(tr '\n' '|' < /tmp/ffipc-ff.log 2>/dev/null | head -c 400)"
if command -v xdotool >/dev/null 2>&1; then
    w=$(timeout 2 xdotool search --class firefox 2>/dev/null | head -1)
    log "ffipc_windows win=${w:-none}"
fi
kill "$ff" 2>/dev/null
sleep 1
pkill -f firefox-esr 2>/dev/null
pkill -f 'Web Content' 2>/dev/null
log "ffipc_done"
echo DAILY_DONE=ok >/dev/console
