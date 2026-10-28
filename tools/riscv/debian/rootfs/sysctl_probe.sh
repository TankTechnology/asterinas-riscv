#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Bounded diagnostic for the /proc/sys writes made by systemd-sysctl.
set -u

log() {
    printf 'SYSCTL_PROBE %s\n' "$*"
}

elapsed_ms() {
    printf '%s' $(( ($2 - $1) / 1000000 ))
}

read_key() {
    name=$1
    path=$2
    start=$(date +%s%N)
    value=$(timeout 15 cat "$path" 2>&1)
    rc=$?
    end=$(date +%s%N)
    log "step=$name rc=$rc ms=$(elapsed_ms "$start" "$end") value=$(printf '%s' "$value" | tr -d '\n')"
}

write_key() {
    name=$1
    path=$2
    value=$3
    start=$(date +%s%N)
    timeout 15 sh -c 'printf "%s\n" "$1" > "$2"' sh "$value" "$path"
    rc=$?
    end=$(date +%s%N)
    log "step=$name rc=$rc ms=$(elapsed_ms "$start" "$end")"
}

run_binary() {
    name=$1
    shift
    start=$(date +%s%N)
    timeout 15 /usr/lib/systemd/systemd-sysctl "$@"
    rc=$?
    end=$(date +%s%N)
    log "step=$name rc=$rc ms=$(elapsed_ms "$start" "$end")"
}

printf 'SYSCTL_PROBE begin\n'
read_key read_pid_max /proc/sys/kernel/pid_max
read_key read_core_pattern /proc/sys/kernel/core_pattern
write_key write_pid_max /proc/sys/kernel/pid_max 4194304
read_key readback_pid_max /proc/sys/kernel/pid_max
write_key write_core_pattern /proc/sys/kernel/core_pattern core
read_key readback_core_pattern /proc/sys/kernel/core_pattern
run_binary binary_core /usr/lib/sysctl.d/10-coredump-debian.conf
run_binary binary_pid /usr/lib/sysctl.d/50-pid-max.conf
run_binary binary_all
read_key missing_control /proc/sys/kernel/asterinas_probe_missing
printf 'SYSCTL_PROBE end\n'
exit 0
