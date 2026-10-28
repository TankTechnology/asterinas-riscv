#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

# The benchmark must execute from the Debian rootfs, not from the initramfs.
# /tmp is shared only as the location of the prepared, Debian-compatible
# benchmark binaries; chroot supplies the process root and Debian libraries.
root=/ext2
chroot_bin=/usr/bin/chroot
bench=/tmp

run_debian() {
    "$chroot_bin" "$root" /bin/sh -c "$1"
}

case ${1:-} in
    syscall-null)
        command="$bench/lat_syscall -P 1 null"
        label="Simple syscall"
        ;;
    process-fork)
        command="$bench/lat_proc -P 1 fork"
        label="Process fork+exit"
        ;;
    process-exec)
        command="cp $bench/hello /tmp/hello; $bench/lat_proc -P 1 exec"
        label="Process fork+execve"
        ;;
    fs-stat)
        command=": > /tmp/lmbench-stat-file; $bench/lat_syscall -P 1 stat /tmp/lmbench-stat-file"
        label="Simple stat"
        ;;
    fs-fstat)
        command=": > /tmp/lmbench-fstat-file; $bench/lat_syscall -P 1 fstat /tmp/lmbench-fstat-file"
        label="Simple fstat"
        ;;
    net-tcp-loopback)
        command="$bench/lat_tcp -s 127.0.0.1 -b 1 >/tmp/lmbench-tcp-server.log 2>&1 & server=\$!; trap 'kill \"\$server\" 2>/dev/null || true' EXIT; sleep 1; $bench/lat_tcp -P 1 127.0.0.1"
        label="TCP latency"
        ;;
    *)
        echo "unknown LMBench case: ${1:-}" >&2
        exit 2
        ;;
esac

printf '%s\n' "LMBENCH_EXECUTION_USERSPACE=debian_chroot"
printf '%s\n' "LMBENCH_LABEL=$label"
run_debian "$command"
