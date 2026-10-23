#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# L5 follow-up: minimise the ext4-journal E2BIG that aborted debootstrap's
# second stage at `/sbin/ldconfig: Writing of cache data failed: Argument list
# too large`.
#
# The probe runs on the guest's root ext2 (L5's target filesystem) and walks
# from benign single operations to a metadata backlog, recording rc and the
# first error line for each step:
#
#   a  ldconfig                     read /etc/ld.so.conf, rebuild the cache
#   b  ldconfig -n /lib /usr/lib    scan directories, ignore the conf
#   c  ldconfig -X                  do not update symlinks
#   d  dd 8 MiB + sync              a large *data* write, little metadata
#   e  touch 2000 files + sync      metadata-heavy, then a plain sync
#   e2 touch 2000 files + fsync     same backlog, but fsync propagates errors
#   f  touch 2000 files + ldconfig  the L5 command after the backlog
#   g  touch E2BIG_LARGE_N (default 12000) files + ldconfig
#     2000 inodes are only ~63 inode-table blocks, below the journal's 253
#     block descriptor, so f is expected to pass; g crosses the capacity.
#
# Console protocol:
#
#   E2BIG step=<a|b|c|d|e|e2|f> rc=<n> dur=<s> [detail=<...>]
#   E2BIG err step=<n> <one-line summary>
#   E2BIG disk_free_kb=<n> phase=<before|after>
#   E2BIG_DONE
#
# Every step is bounded by E2BIG_STEP_TIMEOUT (default 120 s).

DIR=${E2BIG_DIR:-/var/tmp/e2big-probe}
LOGDIR=${E2BIG_LOGDIR:-/var/tmp/e2big-logs}
TOUCH_N=${E2BIG_TOUCH_N:-2000}
LARGE_N=${E2BIG_LARGE_N:-12000}
STEP_TIMEOUT=${E2BIG_STEP_TIMEOUT:-120}

emit() { printf 'E2BIG %s\n' "$*" >/dev/console; }
sanitize() {
    tr '\n\r\t' '   ' | sed 's/[[:space:]]*$//' |
        tr -c 'A-Za-z0-9._=+:/@,-' '_' | cut -c1-200
}
now() { cut -d' ' -f1 /proc/uptime; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }
free_kb() { df -k / 2>/dev/null | awk 'NR==2 { print $4 }'; }

err_of() {
    log=$1
    grep -a -i -E 'error|failed|argument list too long|no space|read-only' "$log" 2>/dev/null |
        tail -1 | sanitize
}

# run_step NAME CMD...: rc, duration, and the first error line on failure.
run_step() {
    name=$1
    shift
    log="$LOGDIR/$name.log"
    t0=$(now)
    timeout -k 5 "$STEP_TIMEOUT" "$@" >"$log" 2>&1 </dev/null
    rc=$?
    dur=$(elapsed "$t0" "$(now)")
    emit "step=$name rc=$rc dur=$dur"
    if [ "$rc" -ne 0 ]; then
        msg=$(err_of "$log")
        [ -n "$msg" ] || msg="rc_${rc}_no_error_line"
        emit "err step=$name $msg"
    fi
    LAST_RC=$rc
    LAST_DUR=$dur
    return 0
}

# touch_many DIR N: create N empty files and report the count.
touch_many() {
    d=$1
    n=$2
    rm -rf "$d"
    mkdir -p "$d" || return 1
    i=1
    while [ "$i" -le "$n" ]; do
        : >"$d/f$i" || return 1
        i=$((i + 1))
    done
    printf '%s' "$n"
}

mkdir -p "$LOGDIR" "$DIR" 2>/dev/null
emit "env kernel=$(uname -r | sanitize) target_fs=$(df -PT / | awk 'NR==2 { print $2 }')"
emit "env dir=$DIR touch_n=$TOUCH_N large_n=$LARGE_N step_timeout=$STEP_TIMEOUT"
emit "disk_free_kb=$(free_kb) phase=before"

# ------------------------------------------------------------------ benign
run_step a ldconfig
run_step b ldconfig -n /lib /usr/lib
run_step c ldconfig -X

rm -rf "$DIR/d"
mkdir -p "$DIR/d"
t0=$(now)
timeout -k 5 "$STEP_TIMEOUT" sh -c \
    "dd if=/dev/zero of='$DIR/d/t.bin' bs=1M count=8 2>'$LOGDIR/d.log' && sync" \
    >"$LOGDIR/d-sync.log" 2>&1 </dev/null
rc=$?
dur=$(elapsed "$t0" "$(now)")
emit "step=d rc=$rc dur=$dur"
if [ "$rc" -ne 0 ]; then
    msg=$(err_of "$LOGDIR/d.log")
    [ -n "$msg" ] || msg=$(err_of "$LOGDIR/d-sync.log")
    [ -n "$msg" ] || msg="rc_${rc}_no_error_line"
    emit "err step=d $msg"
fi

# ------------------------------------------------------- metadata backlog
n=$(touch_many "$DIR/e" "$TOUCH_N")
t0=$(now)
timeout -k 5 "$STEP_TIMEOUT" sh -c \
    "cd '$DIR/e' && : >marker && sync" \
    >"$LOGDIR/e.log" 2>&1 </dev/null
rc=$?
dur=$(elapsed "$t0" "$(now)")
emit "step=e rc=$rc dur=$dur files=$n"
if [ "$rc" -ne 0 ]; then
    msg=$(err_of "$LOGDIR/e.log")
    [ -n "$msg" ] || msg="rc_${rc}_no_error_line"
    emit "err step=e $msg"
fi

# Same backlog, but dd's fsync must surface a writeback error.
n=$(touch_many "$DIR/e2" "$TOUCH_N")
t0=$(now)
timeout -k 5 "$STEP_TIMEOUT" sh -c \
    "dd if=/dev/zero of='$DIR/e2/out.bin' bs=4k count=1 conv=fsync" \
    >"$LOGDIR/e2.log" 2>&1 </dev/null
rc=$?
dur=$(elapsed "$t0" "$(now)")
emit "step=e2 rc=$rc dur=$dur files=$n"
if [ "$rc" -ne 0 ]; then
    msg=$(err_of "$LOGDIR/e2.log")
    [ -n "$msg" ] || msg="rc_${rc}_no_error_line"
    emit "err step=e2 $msg"
fi

# The L5 command, unchanged, but without debootstrap or chroot: a metadata
# backlog followed by ldconfig writing /etc/ld.so.cache.
n=$(touch_many "$DIR/f" "$TOUCH_N")
t0=$(now)
timeout -k 5 "$STEP_TIMEOUT" ldconfig >"$LOGDIR/f.log" 2>&1 </dev/null
rc=$?
dur=$(elapsed "$t0" "$(now)")
emit "step=f rc=$rc dur=$dur files=$n"
if [ "$rc" -ne 0 ]; then
    msg=$(err_of "$LOGDIR/f.log")
    [ -n "$msg" ] || msg="rc_${rc}_no_error_line"
    emit "err step=f $msg"
fi

# A backlog large enough to cross the 253-block descriptor. Spread the files
# over subdirectories so directory insertion stays cheap.
rm -rf "$DIR/g"
mkdir -p "$DIR/g"
i=0
while [ "$i" -lt "$LARGE_N" ]; do
    d="$DIR/g/d$((i / 1000))"
    [ -d "$d" ] || mkdir -p "$d"
    : >"$d/f$i" || break
    i=$((i + 1))
done
t0=$(now)
timeout -k 5 "$STEP_TIMEOUT" ldconfig >"$LOGDIR/g.log" 2>&1 </dev/null
rc=$?
dur=$(elapsed "$t0" "$(now)")
emit "step=g rc=$rc dur=$dur files=$i"
if [ "$rc" -ne 0 ]; then
    msg=$(err_of "$LOGDIR/g.log")
    [ -n "$msg" ] || msg="rc_${rc}_no_error_line"
    emit "err step=g $msg"
fi

# Best effort: drop the probe files again.
rm -rf "$DIR" 2>/dev/null
emit "disk_free_kb=$(free_kb) phase=after"
emit "cache_path=/etc/ld.so.cache cache_kb=$(stat -c %s /etc/ld.so.cache 2>/dev/null || echo 0)"
printf 'E2BIG_DONE\n' >/dev/console
