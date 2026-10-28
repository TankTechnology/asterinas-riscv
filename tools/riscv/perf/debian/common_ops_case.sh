#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

mode=${1:?operation mode is required}
case "$mode" in
    forkexec) iterations=100 ;;
    stat) iterations=1000 ;;
    fstat) iterations=1000 ;;
    fsync) iterations=50 ;;
    *) echo "unsupported operation: $mode" >&2; exit 2 ;;
esac

target=/var/lib/asterinas-perf/common-ops.data
mkdir -p /var/lib/asterinas-perf
printf '%4096s' x > "$target"
start=$(date +%s%N)
i=0
case "$mode" in
    forkexec)
        while [ "$i" -lt "$iterations" ]; do /bin/true; i=$((i + 1)); done
        ;;
    stat)
        while [ "$i" -lt "$iterations" ]; do /usr/bin/stat -c '%s' "$target" >/dev/null; i=$((i + 1)); done
        ;;
    fstat)
        # Asterinas currently does not expose /proc/self/fd symlinks, so use
        # Python's fstat(2) binding to keep this a descriptor-metadata test.
        /usr/bin/python3 - "$target" "$iterations" <<'PY'
import os
import sys

fd = os.open(sys.argv[1], os.O_RDONLY)
for _ in range(int(sys.argv[2])):
    os.fstat(fd)
os.close(fd)
PY
        ;;
    fsync)
        while [ "$i" -lt "$iterations" ]; do
            dd if=/dev/zero of="$target" bs=4096 count=1 conv=fsync status=none
            i=$((i + 1))
        done
        ;;
esac
end=$(date +%s%N)
elapsed_us=$(( (end - start) / 1000 ))
[ "$elapsed_us" -gt 0 ] || elapsed_us=1
printf 'PERF_SAMPLE=%s\n' "$elapsed_us"
