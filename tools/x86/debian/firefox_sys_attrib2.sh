#!/bin/sh
# Firefox syscall histogram via strace started at exec (PTRACE_TRACEME path).
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
export MOZ_CRASHREPORTER_DISABLE=1
export MOZ_DISABLE_CONTENT_SANDBOX=1
log() { printf '%s\n' "$*" >/dev/console; }
log "attr2_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true
timeout 120 apt-get update >/tmp/a2-apt-update.log 2>&1
timeout 240 apt-get install -y --no-install-recommends strace xdotool x11-apps >/tmp/a2-apt.log 2>&1
log "attr2_apt rc=$? strace=$(command -v strace || echo missing)"
if [ ! -S /tmp/.X11-unix/X0 ]; then
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-noudev.conf >/tmp/a2-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
fi
log "attr2_xorg up=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"
python3 - <<'PY'
lines = "".join("<p>line %04d: the quick brown fox jumps over the lazy dog</p>\n" % i for i in range(1, 401))
open("/tmp/a2-page.html", "w").write("<!doctype html><meta charset=utf-8><title>lag</title><body>%s</body>" % lines)
PY
rm -rf /tmp/a2prof && mkdir -p /tmp/a2prof
strace -c -f -o /tmp/a2-strace.txt \
    firefox-esr --no-remote --new-instance --profile /tmp/a2prof /tmp/a2-page.html \
    >/tmp/a2-ff.log 2>&1 &
ff=$!
win=""; i=0
while [ "$i" -lt 120 ]; do
    win=$(timeout 1 xdotool search --class firefox 2>/dev/null | tail -1)
    [ -n "$win" ] && break
    sleep 0.5; i=$((i+1))
done
log "attr2_window win=${win:-none} ff_pid=$ff"
if [ -n "$win" ]; then
    timeout 3 xdotool windowactivate "$win" 2>/dev/null
    sleep 2
    i=0
    while [ "$i" -lt 20 ]; do xdotool key Page_Down 2>/dev/null; i=$((i+1)); done
    sleep 5
fi
kill -TERM "$ff" 2>/dev/null
sleep 3
pkill -f firefox-esr 2>/dev/null
pkill -f 'Web Content' 2>/dev/null
sleep 2
if [ -s /tmp/a2-strace.txt ]; then
    log "attr2_strace_begin"
    head -30 /tmp/a2-strace.txt | sed -E 's/[[:cntrl:]]//g' | while IFS= read -r line; do
        printf '%s\n' "attr2|$line" >/dev/console
    done
    log "attr2_strace_end"
else
    log "attr2_strace_empty err=$(head -2 /tmp/a2-ff.log | tr '\n' '|' | head -c 200)"
fi
log "attr2_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
