#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Firefox sys-time attribution (CPU-side only).
#
# Runs Firefox with Mesa's default software path, attaches strace to all
# Firefox processes for 20 s while scrolling 20 pages, and records:
#   - strace -c per-syscall counts/time
#   - utime/stime deltas (all processes)
#   - minflt/majflt deltas
#
# Markers: sysattr_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
export MOZ_CRASHREPORTER_DISABLE=1
export MOZ_DISABLE_CONTENT_SANDBOX=1

LOG=/tmp/firefox-sysattr.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now_ms() { date +%s%N | cut -c1-13; }

log "sysattr_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true
timeout 120 apt-get update >/tmp/sysattr-apt-update.log 2>&1
timeout 240 apt-get install -y --no-install-recommends strace xdotool x11-apps \
    >/tmp/sysattr-apt.log 2>&1
log "sysattr_apt rc=$? strace=$(command -v strace || echo missing)"

# Xorg: do NOT force softpipe; let Mesa pick llvmpipe (the consistent path).
if [ ! -S /tmp/.X11-unix/X0 ]; then
    /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/sysattr-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
fi
log "sysattr_xorg up=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"

python3 - <<'PY'
lines = "".join("<p>line %04d: the quick brown fox jumps over the lazy dog</p>\n" % i for i in range(1, 401))
open("/tmp/sysattr-page.html", "w").write("<!doctype html><meta charset=utf-8><title>lag</title><body>%s</body>" % lines)
PY

ff_pids() {
    for p in /proc/[0-9]*; do
        [ -r "$p/comm" ] || continue
        case "$(cat "$p/comm" 2>/dev/null)" in
            firefox*|"Web Content"*|RDD*|Socket*) basename "$p" ;;
        esac
    done
}

# ut st minflt majflt  (sum over all firefox processes)
proc_stats() {
    ut=0; st=0; mf=0; Mf=0
    for p in $(ff_pids); do
        set -- $(sed 's/^.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $12, $13, $8, $10}')
        [ -n "${1:-}" ] || continue
        ut=$((ut + $1)); st=$((st + $2)); mf=$((mf + $3)); Mf=$((Mf + $4))
    done
    echo "$ut $st $mf $Mf"
}

rm -rf /tmp/sysattr-prof && mkdir -p /tmp/sysattr-prof
firefox-esr --no-remote --new-instance --profile /tmp/sysattr-prof \
    /tmp/sysattr-page.html >/tmp/sysattr-ff.log 2>&1 &
ff_launch=$!
win=""
i=0
while [ "$i" -lt 120 ]; do
    win=$(timeout 1 xdotool search --class firefox 2>/dev/null | tail -1)
    [ -n "$win" ] && break
    sleep 0.5
    i=$((i + 1))
done
if [ -z "$win" ]; then
    log "sysattr_ff startup=fail log=$(head -3 /tmp/sysattr-ff.log | tr '\n' '|' | head -c 250)"
else
    log "sysattr_ff startup_ok win=$win"
    timeout 3 xdotool windowactivate "$win" 2>/dev/null
    sleep 3

    set -- $(proc_stats); ut0=$1; st0=$2; mf0=$3; Mf0=$4
    log "sysattr_before user_ticks=$ut0 sys_ticks=$st0 minflt=$mf0 majflt=$Mf0 pids=$(ff_pids | tr '\n' ',')"

    pids=""
    for p in $(ff_pids); do pids="$pids -p $p"; done
    if command -v strace >/dev/null 2>&1 && [ -n "$pids" ]; then
        # shellcheck disable=SC2086
        strace -c -f $pids -o /tmp/sysattr-strace.txt >/tmp/sysattr-strace.err 2>&1 &
        strace_pid=$!
        sleep 1
    else
        strace_pid=""
        log "sysattr_strace unavailable or no pids"
    fi

    t2=$(now_ms)
    i=0
    while [ "$i" -lt 20 ]; do
        xdotool key Page_Down 2>/dev/null
        i=$((i + 1))
    done
    sleep 15
    t3=$(now_ms)

    if [ -n "$strace_pid" ]; then
        kill -INT "$strace_pid" 2>/dev/null
        sleep 2
        kill -9 "$strace_pid" 2>/dev/null
    fi

    set -- $(proc_stats); ut1=$1; st1=$2; mf1=$3; Mf1=$4
    log "sysattr_after user_ticks=$ut1 sys_ticks=$st1 minflt=$mf1 majflt=$Mf1"
    log "sysattr_delta user_ticks=$((ut1 - ut0)) sys_ticks=$((st1 - st0)) minflt=$((mf1 - mf0)) majflt=$((Mf1 - Mf0)) window_ms=$((t3 - t2))"

    if [ -s /tmp/sysattr-strace.txt ]; then
        log "sysattr_strace_begin"
        head -25 /tmp/sysattr-strace.txt | sed -E 's/[[:cntrl:]]//g' | while IFS= read -r line; do
            printf '%s\n' "sysattr_strace|$line" >/dev/console
        done
        log "sysattr_strace_end"
    else
        log "sysattr_strace_empty err=$(head -2 /tmp/sysattr-strace.err 2>/dev/null | tr '\n' '|' | head -c 200)"
    fi
fi

kill "$ff_launch" 2>/dev/null
pkill -f firefox-esr 2>/dev/null
pkill -f 'Web Content' 2>/dev/null
log "sysattr_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
