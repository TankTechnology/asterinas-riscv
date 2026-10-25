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
rounds=${STRESS_ROUNDS:-30}

base=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
echo "baseline_memavailable_kb=$base" | tee -a "$LOG" >/dev/console

# Toolchain availability and a one-shot compile, so a missing compiler is
# reported once with versions instead of only as 30 per-round 127s.
gcc_path=$(command -v gcc 2>/dev/null || true)
make_path=$(command -v make 2>/dev/null || true)
gcc_ver=$(gcc --version 2>/dev/null | head -1)
make_ver=$(make --version 2>/dev/null | head -1)
echo "toolchain gcc=${gcc_path:-missing} make=${make_path:-missing}" \
    | tee -a "$LOG" >/dev/console
echo "toolchain_versions gcc=$gcc_ver make=$make_ver" \
    | tee -a "$LOG" >/dev/console
printf '#include <stdio.h>\nint main(){printf("ok\\n");return 0;}\n' >/tmp/t.c
tool_out=$(timeout 30 sh -c 'gcc -O2 /tmp/t.c -o /tmp/t && /tmp/t' 2>&1)
tool_rc=$?
echo "compiler_test rc=$tool_rc out=$(printf '%s' "$tool_out" | tr '\n' ' ')" \
    | tee -a "$LOG" >/dev/console

py_fail=0
gcc_fail=0
gcc_ok=0
round=0
while [ "$round" -lt "$rounds" ]; do
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
    else
        gcc_ok=$((gcc_ok + 1))
        printf 'round=%s gcc_rc=0 ok\n' "$round" >>"$LOG"
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
    echo "stress_failures python3=${py_fail}/${rounds} gcc=${gcc_fail}/${rounds} gcc_ok=${gcc_ok}/${rounds}"
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
