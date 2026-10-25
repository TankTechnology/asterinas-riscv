#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# One-hour soak: 30 rounds of zlib configure/make/test, 512 MiB anonymous
# memory and a 1 GiB write/read-back checksum, padded to ~120 s per round.
# Per-round work time and meminfo are printed to /dev/console and kept in
# /tmp/hour-soak.log.

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG=/tmp/hour-soak.log
: >"$LOG"
TARBALL=/root/zlib-1.3.1.tar.gz

now() { cut -d' ' -f1 /proc/uptime; }
memavail() { awk '/^MemAvailable:/{print $2}' /proc/meminfo; }
loadavg() { cut -d' ' -f1 /proc/loadavg; }

if [ ! -f "$TARBALL" ]; then
    echo "hour_soak=fail missing_tarball=$TARBALL" | tee -a "$LOG" >/dev/console
    echo DAILY_DONE=ok >/dev/console
    exit 0
fi

# Pick a filesystem with room for the 1 GiB file. /dev/shm is tmpfs sized at
# half of RAM; fall back to /tmp.
IOFILE=""
for dir in /dev/shm /tmp; do
    avail=$(df -k "$dir" 2>/dev/null | awk 'NR==2{print $4}')
    if [ -n "$avail" ] && [ "$avail" -ge 1400000 ]; then
        IOFILE="$dir/soak-io.bin"
        break
    fi
done
if [ -z "$IOFILE" ]; then
    echo "hour_soak=fail no_space_for_1GiB" | tee -a "$LOG" >/dev/console
    echo DAILY_DONE=ok >/dev/console
    exit 0
fi

base_ma=$(memavail)
base_up=$(now)
echo "baseline memavail=$base_ma io=$IOFILE uptime=$base_up" | tee -a "$LOG" >/dev/console

cfg_fail=0
make_fail=0
test_fail=0
mem_fail=0
io_fail=0
round=0
while [ "$round" -lt 30 ]; do
    round=$((round + 1))
    t0=$(now)

    # 1) zlib: unpack, configure, build, test.
    rm -rf /tmp/zlib-soak
    mkdir -p /tmp/zlib-soak
    if tar xzf "$TARBALL" -C /tmp/zlib-soak; then
        (
            cd /tmp/zlib-soak/zlib-1.3.1 || exit 1
            ./configure --prefix=/tmp/zi >/tmp/soak-cfg.log 2>&1 || exit 11
            make -j2 >/tmp/soak-make.log 2>&1 || exit 12
            make test >/tmp/soak-test.log 2>&1 || exit 13
        )
        rc=$?
        case "$rc" in
            0) ;;
            11)
                cfg_fail=$((cfg_fail + 1))
                [ -f /tmp/soak-first-cfg.err ] || head -3 /tmp/soak-cfg.log >/tmp/soak-first-cfg.err
                ;;
            12)
                make_fail=$((make_fail + 1))
                [ -f /tmp/soak-first-make.err ] || head -3 /tmp/soak-make.log >/tmp/soak-first-make.err
                ;;
            13)
                test_fail=$((test_fail + 1))
                [ -f /tmp/soak-first-test.err ] || head -3 /tmp/soak-test.log >/tmp/soak-first-test.err
                ;;
            *)
                cfg_fail=$((cfg_fail + 1))
                ;;
        esac
    else
        cfg_fail=$((cfg_fail + 1))
    fi

    # 2) 512 MiB anonymous memory, every page touched.
    if ! timeout 60 python3 -c '
import time
buf = bytearray(512 * 1024 * 1024)
for i in range(0, len(buf), 4096):
    buf[i] = 1
time.sleep(2)
' >/tmp/soak-mem.log 2>&1; then
        mem_fail=$((mem_fail + 1))
    fi

    # 3) 1 GiB file write + read-back checksum.
    io_ok=no
    if dd if=/dev/zero of="$IOFILE" bs=1M count=1024 2>/tmp/soak-dd.log; then
        sha_write=$(sha256sum "$IOFILE" | cut -d' ' -f1)
        sha_read=$(dd if="$IOFILE" bs=1M 2>/dev/null | sha256sum | cut -d' ' -f1)
        if [ "$sha_write" = "$sha_read" ]; then
            io_ok=yes
        else
            io_fail=$((io_fail + 1))
        fi
    else
        io_fail=$((io_fail + 1))
    fi
    rm -f "$IOFILE"

    t1=$(now)
    work_s=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.2f", b-a}')
    ma=$(memavail)
    la=$(loadavg)
    up=$(now)
    echo "round=$round work_s=$work_s memavail=$ma load=$la uptime=$up cfg_fail=$cfg_fail make_fail=$make_fail test_fail=$test_fail mem_fail=$mem_fail io_fail=$io_fail io=$io_ok" | tee -a "$LOG" >/dev/console

    # Pad the round to 120 s; work_s above is what the degradation check uses.
    now_s=$(now)
    pad=$(awk -v a="$t0" -v b="$now_s" 'BEGIN{p=120-(b-a); if(p<0)p=0; printf "%.0f", p}')
    sleep "$pad"
done

last_ma=$(memavail)
end_up=$(now)

{
    echo "=== hour-soak summary ==="
    echo "rounds=30 elapsed_s=$(awk -v a="$base_up" -v b="$end_up" 'BEGIN{printf "%.1f", b-a}')"
    echo "memavail_baseline=$base_ma memavail_last=$last_ma"
    echo "failures cfg=$cfg_fail make=$make_fail test=$test_fail mem=$mem_fail io=$io_fail"
    echo "=== performance (first5 vs last5 work_s) ==="
    awk '/^round=/{n++; for(i=1;i<=NF;i++){if($i ~ /^work_s=/){split($i,a,"="); w[n]=a[2]+0}}} END{for(i=1;i<=5;i++){f+=w[i]; l+=w[n-5+i]} printf "first5_avg=%.2f last5_avg=%.2f ratio=%.3f\n", f/5, l/5, (f>0?l/f:0)}' "$LOG"
    echo "=== first errors ==="
    for f in /tmp/soak-first-cfg.err /tmp/soak-first-make.err /tmp/soak-first-test.err; do
        [ -f "$f" ] && { echo "-- $f"; cat "$f"; }
    done
    echo "=== dmesg scan ==="
    for pat in "Uncaught panic" "oom-killer" "Out of memory" "allocation failed"; do
        c=$(dmesg | grep -c -- "$pat" 2>/dev/null || true)
        echo "dmesg_count[$pat]=$c"
    done
    echo "=== rounds ==="
    grep '^round=' "$LOG"
} >/dev/console 2>&1

echo DAILY_DONE=ok >/dev/console
