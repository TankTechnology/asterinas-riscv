#!/bin/sh
# Clean sys/user split for a fixed Firefox pid set over a 20-page scroll.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
export MOZ_CRASHREPORTER_DISABLE=1
export MOZ_DISABLE_CONTENT_SANDBOX=1
log() { printf '%s\n' "$*" >/dev/console; }
log "fixed_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if [ ! -S /tmp/.X11-unix/X0 ]; then
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-noudev.conf >/tmp/fx-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
fi
python3 - <<'PY'
lines = "".join("<p>line %04d: the quick brown fox jumps over the lazy dog</p>\n" % i for i in range(1, 401))
open("/tmp/fx-page.html", "w").write("<!doctype html><meta charset=utf-8><title>lag</title><body>%s</body>" % lines)
PY
rm -rf /tmp/fxprof && mkdir -p /tmp/fxprof
firefox-esr --no-remote --new-instance --profile /tmp/fxprof /tmp/fx-page.html >/tmp/fx-ff.log 2>&1 &
ff=$!
win=""; i=0
while [ "$i" -lt 120 ]; do
    win=$(timeout 1 xdotool search --class firefox 2>/dev/null | tail -1)
    [ -n "$win" ] && break
    sleep 0.5; i=$((i+1))
done
if [ -z "$win" ]; then
    log "fixed_window none log=$(head -2 /tmp/fx-ff.log | tr '\n' '|' | head -c 200)"
    kill "$ff" 2>/dev/null
    echo DAILY_DONE=ok >/dev/console
    exit 0
fi
timeout 3 xdotool windowactivate "$win" 2>/dev/null
sleep 3
# Freeze the pid set.
PIDS=""
for p in /proc/[0-9]*; do
    [ -r "$p/comm" ] || continue
    case "$(cat "$p/comm" 2>/dev/null)" in
        firefox*|"Web Content"*|RDD*|Socket*) PIDS="$PIDS ${p#/proc/}" ;;
    esac
done
sum_for() {
    ut=0; st=0; mf=0; Mf=0
    for p in $PIDS; do
        set -- $(sed 's/^.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $12, $13, $8, $10}')
        [ -n "${1:-}" ] || continue
        ut=$((ut + $1)); st=$((st + $2)); mf=$((mf + $3)); Mf=$((Mf + $4))
    done
    echo "$ut $st $mf $Mf"
}
log "fixed_pids count=$(echo $PIDS | wc -w) pids=$(echo $PIDS | tr ' ' ',')"
set -- $(sum_for); ut0=$1; st0=$2; mf0=$3; Mf0=$4
i=0
while [ "$i" -lt 20 ]; do xdotool key Page_Down 2>/dev/null; i=$((i+1)); done
sleep 5
set -- $(sum_for); ut1=$1; st1=$2; mf1=$3; Mf1=$4
log "fixed_delta user_ticks=$((ut1 - ut0)) sys_ticks=$((st1 - st0)) minflt=$((mf1 - mf0)) majflt=$((Mf1 - Mf0))"
log "fixed_total user_ticks=$ut1 sys_ticks=$st1 minflt=$mf1 majflt=$Mf1"
kill "$ff" 2>/dev/null
pkill -f firefox-esr 2>/dev/null
pkill -f 'Web Content' 2>/dev/null
log "fixed_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
