#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Phase-1 baseline for the x86 CPU-side display and daily-use performance
# work.  No GPU driver code is involved: everything measures the fb0 /
# llvmpipe / CPU path.
#
# A. display: fb mmap bandwidth, glxgears FPS, full-screen redraw, window move
# B. daily: fork+exec, getpid, memcpy, dd, find, zlib -j4, python startup
# C. audit: /proc/cpuinfo flags, XCR0 via xgetbv, AVX2/AVX-512 execution,
#    nr_cpus topology, THP
#
# Markers: perf_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DISPLAY=:0
export HOME=/tmp

LOG=/tmp/perf-baseline.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }
now_ns() { date +%s%N; }
ms_between() { awk -v a="$1" -v b="$2" 'BEGIN{printf "%.1f", (b-a)/1000000}'; }

log "perf_baseline start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true

# The baseline runs its own Xorg on :0 (the gate does not start one).
if [ ! -S /tmp/.X11-unix/X0 ]; then
    GALLIUM_DRIVER=softpipe LIBGL_ALWAYS_SOFTWARE=1 \
        /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
        -config /etc/X11/xorg-noudev.conf >/tmp/perf-xorg.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do
        [ -S /tmp/.X11-unix/X0 ] && break
        sleep 0.5
        i=$((i + 1))
    done
fi
if [ -S /tmp/.X11-unix/X0 ]; then
    log "perf_xorg up=yes driver_log=$(grep -E 'Using|modesetting|fbdev|glamor' /tmp/perf-xorg.log 2>/dev/null | head -3 | tr '\n' '|' | head -c 240)"
else
    log "perf_xorg up=no log=$(head -3 /tmp/perf-xorg.log 2>/dev/null | tr '\n' '|' | head -c 240)"
fi
log "perf_dev_dri=$(ls /dev/dri 2>&1 | tr '\n' ' ' | head -c 120) fb=$(ls /dev/fb* 2>&1 | tr '\n' ' ' | head -c 120)"

timeout 180 apt-get update >/tmp/perf-apt-update.log 2>&1
timeout 300 apt-get install -y --no-install-recommends x11-apps x11-xserver-utils xdotool \
    >/tmp/perf-apt.log 2>&1
log "perf_apt rc=$? glxgears=$(command -v glxgears || echo missing) xdotool=$(command -v xdotool || echo missing)"

# ---------------------------------------------------------------- C. audit
log "perf_cpu_nproc=$(nproc 2>&1) online=$(getconf _NPROCESSORS_ONLN 2>&1)"
log "perf_cpu_cpuinfo_processors=$(grep -c '^processor' /proc/cpuinfo 2>&1)"
log "perf_cpu_model=$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')"
log "perf_cpu_flags=$(grep -m1 '^flags' /proc/cpuinfo | cut -d: -f2- | tr ' ' '\n' | grep -E '^(sse2|ssse3|sse4_1|sse4_2|avx|avx2|fma|avx512f|avx512vl|avx512bw|avx512dq|avx512_vnni|xsave|osxsave|rdrand|rdseed)$' | tr '\n' ',' | head -c 300)"
log "perf_cpu_topology=$(grep -c '^processor' /proc/cpuinfo) cpus; cmdline=$(tr ' ' ' ' </proc/cmdline | head -c 200)"
log "perf_thp_enabled=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>&1 | head -c 120)"
log "perf_mem_total_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo) anonhuge_kb=$(awk '/^AnonHugePages:/{print $2}' /proc/meminfo)"

mkdir -p /tmp/perfbin
cat >/tmp/perfbin/xgetbv.c <<'C'
#include <stdio.h>
int main(void) {
    unsigned int eax, edx;
    __asm__ volatile("xgetbv" : "=a"(eax), "=d"(edx) : "c"(0));
    printf("xcr0=0x%08x%08x\n", edx, eax);
    return 0;
}
C
cat >/tmp/perfbin/avx2.c <<'C'
#include <immintrin.h>
#include <stdio.h>
int main(void) {
    __m256 a = _mm256_set1_ps(1.5f), acc = _mm256_setzero_ps();
    for (int i = 0; i < 1000000; i++) acc = _mm256_add_ps(acc, a);
    float out[8]; _mm256_storeu_ps(out, acc);
    printf("avx2_ok=%f\n", out[0]);
    return 0;
}
C
cat >/tmp/perfbin/avx512.c <<'C'
#include <immintrin.h>
#include <stdio.h>
int main(void) {
    __m512 a = _mm512_set1_ps(1.5f), acc = _mm512_setzero_ps();
    for (int i = 0; i < 500000; i++) acc = _mm512_add_ps(acc, a);
    float out[16]; _mm512_storeu_ps(out, acc);
    printf("avx512_ok=%f\n", out[0]);
    return 0;
}
C
cat >/tmp/perfbin/getpid.c <<'C'
#include <stdio.h>
#include <unistd.h>
int main(void) {
    volatile pid_t p = 0;
    for (int i = 0; i < 1000000; i++) p = getpid();
    printf("last=%d\n", (int)p);
    return 0;
}
C
cat >/tmp/perfbin/memcpy_bw.c <<'C'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
int main(void) {
    size_t n = 512UL << 20;
    char *a = malloc(n), *b = malloc(n);
    if (!a || !b) { puts("alloc_failed"); return 1; }
    memset(a, 1, n);
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    memcpy(b, a, n);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    double s = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
    printf("memcpy_512mib_sec=%.3f gib_per_sec=%.2f sum=%d\n", s, 0.5 / s, b[n/2]);
    return 0;
}
C
gcc -O2 -o /tmp/perfbin/xgetbv /tmp/perfbin/xgetbv.c 2>/tmp/perfbin/cc-xgetbv.err
log "perf_build_xgetbv rc=$? err=$(head -1 /tmp/perfbin/cc-xgetbv.err | head -c 120)"
out=$(/tmp/perfbin/xgetbv 2>&1); rcx=$?
log "perf_xgetbv rc=$rcx out=$(printf '%s' "$out" | head -c 120)"
gcc -O2 -mavx2 -o /tmp/perfbin/avx2 /tmp/perfbin/avx2.c 2>/tmp/perfbin/cc-avx2.err
log "perf_build_avx2 rc=$? err=$(head -1 /tmp/perfbin/cc-avx2.err | head -c 120)"
out=$(/tmp/perfbin/avx2 2>&1); rcavx=$?
log "perf_avx2_exec rc=$rcavx out=$(printf '%s' "$out" | head -c 120)"
gcc -O2 -mavx512f -o /tmp/perfbin/avx512 /tmp/perfbin/avx512.c 2>/tmp/perfbin/cc-avx512.err
log "perf_build_avx512 rc=$? err=$(head -1 /tmp/perfbin/cc-avx512.err | head -c 120)"
out=$(/tmp/perfbin/avx512 2>&1); rc512=$?
log "perf_avx512_exec rc=$rc512 out=$(printf '%s' "$out" | head -c 120)"

# ---------------------------------------------------------------- A. display
fb_virtual=$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null | tr ',' 'x')
fb_bpp=$(cat /sys/class/graphics/fb0/bits_per_pixel 2>/dev/null)
log "perf_fb geometry=$fb_virtual bpp=$fb_bpp"
if [ -e /dev/fb0 ]; then
    python3 - "$fb_virtual" "$fb_bpp" <<'PY'
import mmap, os, statistics, sys, time
geom, bpp = sys.argv[1], int(sys.argv[2] or "32")
w, h = (int(x) for x in geom.split("x"))
frame = w * h * bpp // 8
fd = os.open("/dev/fb0", os.O_RDWR)
try:
    mm = mmap.mmap(fd, frame, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    samples = []
    screens = 10
    for _ in range(5):
        t0 = time.monotonic()
        for _ in range(screens):
            mm[:] = bytes([0x5a]) * frame
        mm.flush()
        samples.append(time.monotonic() - t0)
    best = min(samples)
    print("perf_fb_write screens=%d frame_bytes=%d best_sec=%.4f mib_per_sec=%.1f"
          % (screens, frame, best, screens * frame / (1 << 20) / best))
finally:
    os.close(fd)
PY
else
    log "perf_fb_write missing /dev/fb0"
fi

if [ -S /tmp/.X11-unix/X0 ] && command -v glxinfo >/dev/null 2>&1; then
    log "perf_gl_renderer=$(glxinfo -B 2>/dev/null | grep -E 'OpenGL renderer|OpenGL version' | tr '\n' '|' | head -c 200)"
fi
if [ -S /tmp/.X11-unix/X0 ] && command -v glxgears >/dev/null 2>&1; then
    glx_out=$(timeout 40 glxgears -info 2>&1 | tail -20)
    log "perf_glxgears=$(printf '%s' "$glx_out" | grep -E 'frames in|GL_RENDERER' | tr '\n' '|' | head -c 500)"
else
    log "perf_glxgears missing"
fi

if [ -S /tmp/.X11-unix/X0 ] && command -v xsetroot >/dev/null 2>&1; then
    t0=$(now_ns)
    i=0
    while [ "$i" -lt 20 ]; do
        if [ $((i % 2)) -eq 0 ]; then
            xsetroot -solid '#335577' 2>/dev/null
        else
            xsetroot -solid '#775533' 2>/dev/null
        fi
        i=$((i + 1))
    done
    t1=$(now_ns)
    log "perf_redraw rounds=20 total_ms=$(ms_between "$t0" "$t1")"
else
    log "perf_redraw skipped: xorg-or-xsetroot-missing"
fi

xclock >/dev/null 2>&1 &
clock_pid=$!
win=""
i=0
while [ "$i" -lt 20 ]; do
    win=$(timeout 1 xdotool search --class xclock 2>/dev/null | head -1)
    [ -n "$win" ] && break
    sleep 1
    i=$((i + 1))
done
if [ -n "$win" ]; then
    t0=$(now_ns)
    i=0
    while [ "$i" -lt 100 ]; do
        xdotool windowmove "$win" $((50 + i)) $((40 + i)) 2>/dev/null
        i=$((i + 1))
    done
    t1=$(now_ns)
    log "perf_move moves=100 win=$win total_ms=$(ms_between "$t0" "$t1")"
else
    log "perf_move window-missing"
fi
kill "$clock_pid" 2>/dev/null

# ---------------------------------------------------------------- B. daily
t0=$(now_ns)
python3 - <<'PY'
import subprocess
for _ in range(200):
    subprocess.run(["true"], check=False)
PY
t1=$(now_ns)
log "perf_fork_exec ops=200 total_ms=$(ms_between "$t0" "$t1")"

gcc -O2 -o /tmp/perfbin/getpid /tmp/perfbin/getpid.c 2>/dev/null
t0=$(now_ns); /tmp/perfbin/getpid >/dev/null 2>&1; t1=$(now_ns)
log "perf_getpid ops=1000000 total_ms=$(ms_between "$t0" "$t1")"

gcc -O2 -o /tmp/perfbin/memcpy_bw /tmp/perfbin/memcpy_bw.c 2>/dev/null
log "perf_memcpy $(/tmp/perfbin/memcpy_bw 2>&1 | head -c 160)"

t0=$(now_ns)
dd if=/dev/zero of=/tmp/perf-dd.bin bs=1M count=512 conv=fsync 2>/tmp/perf-dd-w.err
wrc=$?
t1=$(now_ns)
log "perf_dd_write mb=512 rc=$wrc total_ms=$(ms_between "$t0" "$t1") rate=$(tail -1 /tmp/perf-dd-w.err | tr -s ' ' | head -c 80)"
t0=$(now_ns)
dd if=/tmp/perf-dd.bin of=/dev/null bs=1M iflag=direct 2>/tmp/perf-dd-r.err
rrc=$?
t1=$(now_ns)
log "perf_dd_read mb=512 rc=$rrc total_ms=$(ms_between "$t0" "$t1") rate=$(tail -1 /tmp/perf-dd-r.err | tr -s ' ' | head -c 80)"
rm -f /tmp/perf-dd.bin

t0=$(now_ns)
n=$(find /usr -xdev -type f 2>/dev/null | wc -l)
t1=$(now_ns)
log "perf_find files=$n total_ms=$(ms_between "$t0" "$t1")"

t0=$(now_ns)
python3 - <<'PY'
import subprocess, time
t = 0.0
for _ in range(20):
    s = time.monotonic(); subprocess.run(["python3", "-c", "pass"], check=False); t += time.monotonic() - s
print("perf_python_start runs=20 avg_ms=%.1f" % (t / 20 * 1000))
PY
t1=$(now_ns)
log "perf_python_start_wall total_ms=$(ms_between "$t0" "$t1")"

# zlib -j4 build (the tarball ships with the image used by dev_workflow)
if [ -f /root/zlib-1.3.1.tar.gz ]; then
    rm -rf /tmp/zlib-perf && mkdir -p /tmp/zlib-perf
    tar xzf /root/zlib-1.3.1.tar.gz -C /tmp/zlib-perf
    (
        cd /tmp/zlib-perf/zlib-1.3.1 || exit 1
        t0=$(now_ns)
        ./configure >/tmp/zlib-cfg.log 2>&1
        cfg=$?
        make -j4 >/tmp/zlib-make.log 2>&1
        mk=$?
        t1=$(now_ns)
        echo "perf_zlib cfg_rc=$cfg make_rc=$mk build_ms=$(ms_between "$t0" "$t1")"
    )
else
    log "perf_zlib tarball-missing"
fi

log "perf_baseline_done=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
