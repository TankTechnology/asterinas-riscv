#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_SAFE_REBOOT_CONSOLE:-/dev/console}"
readonly DEADLINE="${ASTERINAS_SAFE_REBOOT_AFTER:-}"
readonly UPTIME_FILE="${ASTERINAS_SAFE_REBOOT_UPTIME_FILE:-/proc/uptime}"

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
read -r uptime _ <"$UPTIME_FILE" || fail uptime-read
[[ "$uptime" =~ ^(0|[1-9][0-9]*)\.[0-9]+$ ]] || fail invalid-uptime
readonly uptime_seconds="${uptime%%.*}"
readonly remaining=$((DEADLINE > uptime_seconds ? DEADLINE - uptime_seconds : 0))

emit "ASTERINAS_USERSPACE_REBOOT_ARMED uptime=$uptime_seconds deadline=$DEADLINE"
if ((remaining > 0)); then
    sleep "$remaining" || fail sleep
fi
sync || fail sync
emit "ASTERINAS_USERSPACE_REBOOT_SYNC deadline=$DEADLINE"
reboot -f || fail reboot
