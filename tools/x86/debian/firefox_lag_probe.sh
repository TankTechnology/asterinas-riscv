#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Firefox lag probe (CPU-side only; no GPU work).
#
# 1. Verify the AVX/XCR0 claim: /proc/cpuinfo flags, xgetbv, AVX2 execution.
# 2. Capture the llvmpipe renderer (glxinfo, with a hard timeout: Mesa talks
#    to the fake card0 and may block on an unimplemented DRM ioctl).
# 3. Run Firefox three times (A default, B no-WebRender/no-layers-accel,
#    C software-WebRender + fewer content processes) on a local page and
#    measure window startup and 20x Page_Down scroll time, plus the
#    Firefox processes' user vs system CPU time from /proc/<pid>/stat.
#
# Markers: ff_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp
export MOZ_CRASHREPORTER_DISABLE=1
export MOZ_DISABLE_CONTENT_SANDBOX=1
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=softpipe

LOG=/tmp/firefox-lag.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now_ms() { date +%s%N | cut -c1-13; }

log "ff_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true
timeout 240 apt-get update >/tmp/ff-apt-update.log 2>&1
timeout 600 apt-get install -y --no-install-recommends firefox-esr x11-apps \
    x11-xserver-utils xdotool >/tmp/ff-apt.log 2>&1
apt_rc=$?
log "ff_apt rc=$apt_rc firefox=$(command -v firefox-esr || echo missing) glxinfo=$(command -v glxinfo || echo missing)"

# CPU capability evidence.
log "ff_cpu_flags=$(grep -m1 '^flags' /proc/cpuinfo | cut -d: -f2- | tr ' ' '\n' | grep -E '^(sse2|sse4_1|sse4_2|avx|avx2|fma|avx512f|xsave|pclmulqdq)$' | tr '\n' ',' | head -c 200)"
printf 'int main(void){unsigned a,d;__asm__ volatile("xgetbv":"=a"(a),"=d"(d):"c"(0));printf("xcr0=0x%%08x%%08x\\n",d,a);return 0;}\n' >/tmp/xgetbv.c
gcc -O2 -o /tmp/xgetbv /tmp/xgetbv.c 2>/dev/null
log "ff_xgetbv rc=$? out=$(/tmp/xgetbv 2>&1 | head -1)"

cat >/tmp/avx2probe.c <<'C'
#include <immintrin.h>
#include <stdio.h>
int main(void){__m256 a=_mm256_set1_ps(2.0f),s=_mm256_setzero_ps();
 for(int i=0;i<1000000;i++) s=_mm256_add_ps(s,a);
 float o[8];_mm256_storeu_ps(o,s);printf("avx2=%.0f\n",o[0]);return 0;}
C
gcc -O2 -mavx2 -o /tmp/avx2probe /tmp/avx2probe.c 2>/dev/null
log "ff_avx2 rc=$? out=$(/tmp/avx2probe 2>&1 | head -1)"

# Xorg (the gate starts none for this checklist).
if [ ! -S /tmp/.X11-unix/X0 ]; then
    GALLIUM_DRIVER=softpipe LIBGL_ALWAYS_SOFTWARE=1 \
        /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/ff-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.5; i=$((i+1)); done
fi
log "ff_xorg up=$([ -S /tmp/.X11-unix/X0 ] && echo yes || echo no)"
log "ff_dev_dri=$(ls /dev/dri 2>&1 | tr '\n' ' ') fb=$(ls /dev/fb* 2>&1 | tr '\n' ' ')"

if command -v glxinfo >/dev/null 2>&1; then
    gx=$(timeout 20 glxinfo -B 2>&1); gxrc=$?
    log "ff_glxinfo rc=$gxrc renderer=$(printf '%s' "$gx" | grep -E 'OpenGL renderer|OpenGL version' | tr '\n' '|' | head -c 240)"
else
    log "ff_glxinfo missing"
fi

# Local page and measurement helpers.
python3 - <<'PY'
lines = "".join("<p>line %04d: the quick brown fox jumps over the lazy dog</p>\n" % i for i in range(1, 401))
open("/tmp/ff-page.html", "w").write("<!doctype html><meta charset=utf-8><title>lag</title><body>%s</body>" % lines)
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
        # /proc/<pid>/stat field 2 (comm) may contain spaces; cut through the
        # last ')' first, then utime/stime are fields 12/13 of the remainder.
        set -- $(sed 's/^.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $12, $13}')
        [ -n "${1:-}" ] && ut=$((ut + $1)) && st=$((st + $2))
    done
    echo "$ut $st"
}

run_group() {
    tag=$1
    prefs=$2
    rm -rf "/tmp/ffprof-$tag"
    mkdir -p "/tmp/ffprof-$tag"
    if [ -n "$prefs" ]; then
        printf '%s\n' "$prefs" >"/tmp/ffprof-$tag/user.js"
    fi
    firefox-esr --no-remote --new-instance --profile "/tmp/ffprof-$tag" \
        /tmp/ff-page.html >/tmp/ff-$tag.log 2>&1 &
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
        log "ff_$tag startup=fail launch_ms=$((t1 - t0)) log=$(head -2 /tmp/ff-$tag.log | tr '\n' '|' | head -c 200)"
        kill "$ff_launch" 2>/dev/null
        pkill -f firefox-esr 2>/dev/null
        pkill -f 'Web Content' 2>/dev/null
        return
    fi
    log "ff_$tag startup_ok launch_ms=$((t1 - t0)) win=$win"
    timeout 3 xdotool windowactivate "$win" 2>/dev/null
    sleep 2
    set -- $(cpu_times); ut0=$1; st0=$2
    t2=$(now_ms)
    i=0
    while [ "$i" -lt 20 ]; do
        xdotool key Page_Down 2>/dev/null
        i=$((i + 1))
    done
    t3=$(now_ms)
    sleep 1
    set -- $(cpu_times); ut1=$1; st1=$2
    log "ff_$tag scroll20_ms=$((t3 - t2)) user_ticks=$((ut1 - ut0)) sys_ticks=$((st1 - st0)) ticks_per_sec=$(getconf CLK_TCK 2>/dev/null)"
    kill "$ff_launch" 2>/dev/null
    pkill -f firefox-esr 2>/dev/null
    pkill -f 'Web Content' 2>/dev/null
    sleep 2
}

run_group A ""
run_group B 'user_pref("gfx.webrender.software", false);
user_pref("gfx.webrender.all", false);
user_pref("layers.acceleration.disabled", true);
user_pref("layers.acceleration.force-enabled", false);'
run_group C 'user_pref("gfx.webrender.software", true);
user_pref("dom.ipc.processCount", 2);'

log "ff_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
