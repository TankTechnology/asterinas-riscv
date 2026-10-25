#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# 30-round memory/stability stress for the Debian guest.
#
# Each round allocates and touches 512 MiB, compiles and runs a tiny C file,
# samples /proc/meminfo, then sleeps 10 s. Everything is teed into
# /tmp/stress.log and printed to /dev/console so the host can capture it.
# The final block reports failure counts and dmesg markers for panic, OOM and
# kernel allocation failures.

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG=/tmp/stress.log
: >"$LOG"

base=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
echo "baseline_memavailable_kb=$base" | tee -a "$LOG" >/dev/console

py_fail=0
gcc_fail=0
round=0
while [ "$round" -lt 30 ]; do
    round=$((round + 1))

    # 1) Memory pressure: allocate 512 MiB and touch every page. The timeout
    # only bounds a pathological round; the process exits on its own.
    py_err=$(timeout 50 python3 -c '
import time
buf = bytearray(512 * 1024 * 1024)
for i in range(0, len(buf), 4096):
    buf[i] = 1
time.sleep(5)
' 2>&1)
    py_rc=$?
    if [ "$py_rc" -ne 0 ]; then
        py_fail=$((py_fail + 1))
        printf 'round=%s python3_rc=%s err=%s\n' "$round" "$py_rc" \
            "$(printf '%s' "$py_err" | head -1)" >>"$LOG"
    fi

    # 2) Compile pressure.
    cc_err=$(timeout 40 sh -c 'cd /tmp && printf "%s\n" "#include <stdio.h>" "int main(){return 0;}" > t.c && gcc -O2 t.c -o t && ./t' 2>&1)
    cc_rc=$?
    if [ "$cc_rc" -ne 0 ]; then
        gcc_fail=$((gcc_fail + 1))
        printf 'round=%s gcc_rc=%s err=%s\n' "$round" "$cc_rc" \
            "$(printf '%s' "$cc_err" | head -1)" >>"$LOG"
    fi

    # 3) Sample.
    ma=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
    ca=$(awk '/^Cached:/{print $2}' /proc/meminfo)
    echo "t=${round}min memavail=$ma caches=$ca" | tee -a "$LOG" >/dev/console

    sleep 10
done

last=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
dmesg | tail -40 >/tmp/stress-dmesg.txt

{
    echo "=== stress.log begin ==="
    cat "$LOG"
    echo "=== stress.log end ==="
    echo "stress_failures python3=${py_fail}/30 gcc=${gcc_fail}/30"
    echo "baseline_memavailable_kb=$base last_memavailable_kb=$last"
    echo "=== first three errors ==="
    grep -m 3 'err=' "$LOG" 2>/dev/null || true
    echo "=== dmesg scan ==="
    for pat in "Uncaught panic" "oom-killer" "Out of memory" "allocation failed"; do
        count=$(dmesg | grep -c -- "$pat" 2>/dev/null || true)
        echo "dmesg_count[$pat]=$count"
    done
    echo "=== dmesg tail ==="
    cat /tmp/stress-dmesg.txt
} >/dev/console 2>&1

echo DAILY_DONE=ok >/dev/console
