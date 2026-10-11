#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

case ${1:-} in
    syscall-null)
        command="/benchmark/bin/lmbench/lat_syscall -P 1 null"
        label="Simple syscall"
        ;;
    process-fork)
        command="/benchmark/bin/lmbench/lat_proc -P 1 fork"
        label="Process fork+exit"
        ;;
    process-exec)
        cp /benchmark/bin/lmbench/hello /tmp/hello
        command="/benchmark/bin/lmbench/lat_proc -P 1 exec"
        label="Process fork+execve"
        ;;
    fs-stat)
        : > /ext2/lmbench-stat-file
        command="/benchmark/bin/lmbench/lat_syscall -P 1 stat /ext2/lmbench-stat-file"
        label="Simple stat"
        ;;
    fs-fstat)
        : > /ext2/lmbench-fstat-file
        command="/benchmark/bin/lmbench/lat_syscall -P 1 fstat /ext2/lmbench-fstat-file"
        label="Simple fstat"
        ;;
    net-tcp-loopback)
        /benchmark/bin/lmbench/lat_tcp -s 127.0.0.1 -b 1 >/tmp/lmbench-tcp-server.log 2>&1 &
        server=$!
        trap 'kill "$server" 2>/dev/null || true' EXIT
        sleep 1
        command="/benchmark/bin/lmbench/lat_tcp -P 1 127.0.0.1"
        label="TCP latency"
        ;;
    *)
        echo "unknown LMBench case: ${1:-}" >&2
        exit 2
        ;;
esac

printf '%s\n' "LMBENCH_LABEL=$label"
sh -c "$command"
