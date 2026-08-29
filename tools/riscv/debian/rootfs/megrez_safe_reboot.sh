#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_SAFE_REBOOT_CONSOLE:-/dev/console}"
readonly DEADLINE="${ASTERINAS_SAFE_REBOOT_AFTER:-}"
readonly UPTIME_FILE="${ASTERINAS_SAFE_REBOOT_UPTIME_FILE:-/proc/uptime}"
readonly MAX_SLEEP_SECONDS=5

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "ASTERINAS_USERSPACE_REBOOT_FAIL reason=$1"
    exit 1
}

[[ -n "$DEADLINE" ]] || exit 0
[[ "$DEADLINE" =~ ^[1-9][0-9]*$ ]] || fail invalid-deadline
[[ "$UPTIME_FILE" == /* && -f "$UPTIME_FILE" && ! -L "$UPTIME_FILE" ]] ||
    fail invalid-uptime-file

read_uptime_seconds() {
    local uptime

    read -r uptime _ <"$UPTIME_FILE" || fail uptime-read
    [[ "$uptime" =~ ^(0|[1-9][0-9]*)\.[0-9]+$ ]] || fail invalid-uptime
    uptime_seconds="${uptime%%.*}"
}

uptime_seconds=0
read_uptime_seconds

emit "ASTERINAS_USERSPACE_REBOOT_ARMED uptime=$uptime_seconds deadline=$DEADLINE"
while ((uptime_seconds < DEADLINE)); do
    remaining=$((DEADLINE - uptime_seconds))
    sleep_seconds=$((remaining < MAX_SLEEP_SECONDS ? remaining : MAX_SLEEP_SECONDS))
    sleep "$sleep_seconds" || fail sleep
    previous_uptime_seconds=$uptime_seconds
    read_uptime_seconds
    ((uptime_seconds >= previous_uptime_seconds)) || fail uptime-regressed
done
sync || fail sync
emit "ASTERINAS_USERSPACE_REBOOT_SYNC deadline=$DEADLINE"
reboot -f || fail reboot
