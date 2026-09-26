#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_SAFE_REBOOT_CONSOLE:-/dev/console}"
readonly CMDLINE_PATH="${ASTERINAS_SAFE_REBOOT_CMDLINE_PATH:-/proc/cmdline}"
readonly UPTIME_FILE="${ASTERINAS_SAFE_REBOOT_UPTIME_FILE:-/proc/uptime}"
readonly MAX_SLEEP_SECONDS=5
readonly KERNEL_REBOOT_GUARD_SECONDS=180
readonly QUIESCE_TIMEOUT_SECONDS=60
readonly UNIT_KILL_TIMEOUT_SECONDS=15
readonly TERM_WAIT_SECONDS="${ASTERINAS_SAFE_REBOOT_TERM_WAIT_SECONDS:-30}"
readonly KILL_WAIT_SECONDS="${ASTERINAS_SAFE_REBOOT_KILL_WAIT_SECONDS:-15}"
readonly SYNC_TIMEOUT_SECONDS=45
readonly KERNEL_DEADLINE_RESERVE_SECONDS=15
readonly SYNC_RESERVE_SECONDS=20
readonly USER_ID=1000
readonly UNIT_DIR="${ASTERINAS_SAFE_REBOOT_UNIT_DIR:-/etc/systemd/system}"
readonly -a WRITE_HEAVY_UNITS=(
    asterinas-browser-web.service
    asterinas-desktop-m5-network.service
    asterinas-desktop-m5.service
)

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    emit "ASTERINAS_USERSPACE_REBOOT_FAIL reason=$1"
    exit 1
}

[[ "$TERM_WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-term-wait
[[ "$KILL_WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-kill-wait

DEADLINE="${ASTERINAS_SAFE_REBOOT_AFTER:-}"
KERNEL_DEADLINE=''
[[ "$CMDLINE_PATH" == /* && -f "$CMDLINE_PATH" && ! -L "$CMDLINE_PATH" ]] ||
    fail invalid-cmdline-file
read -r -a cmdline_tokens <"$CMDLINE_PATH" || fail cmdline-read
for token in "${cmdline_tokens[@]}"; do
    [[ "$token" == asterinas.reboot_after=* ]] || continue
    [[ -z "$KERNEL_DEADLINE" ]] || fail ambiguous-kernel-deadline
    KERNEL_DEADLINE="${token#asterinas.reboot_after=}"
done
if [[ -n "$KERNEL_DEADLINE" ]]; then
    [[ "$KERNEL_DEADLINE" =~ ^[1-9][0-9]*$ ]] || fail invalid-kernel-deadline
fi
if [[ -z "$DEADLINE" ]]; then
    [[ -n "$KERNEL_DEADLINE" ]] || exit 0
    DEADLINE=$((KERNEL_DEADLINE > KERNEL_REBOOT_GUARD_SECONDS ?
        KERNEL_DEADLINE - KERNEL_REBOOT_GUARD_SECONDS : 1))
fi
readonly DEADLINE KERNEL_DEADLINE
[[ "$DEADLINE" =~ ^[1-9][0-9]*$ ]] || fail invalid-deadline
SOFT_DEADLINE=0
if [[ -n "$KERNEL_DEADLINE" ]]; then
    ((KERNEL_DEADLINE > KERNEL_DEADLINE_RESERVE_SECONDS)) ||
        fail insufficient-kernel-deadline
    SOFT_DEADLINE=$((KERNEL_DEADLINE - KERNEL_DEADLINE_RESERVE_SECONDS))
fi
readonly SOFT_DEADLINE
[[ "$UPTIME_FILE" == /* && -f "$UPTIME_FILE" && ! -L "$UPTIME_FILE" ]] ||
    fail invalid-uptime-file

read_uptime_seconds() {
    local uptime

    read -r uptime _ <"$UPTIME_FILE" || fail uptime-read
    [[ "$uptime" =~ ^(0|[1-9][0-9]*)\.[0-9]+$ ]] || fail invalid-uptime
    uptime_seconds="${uptime%%.*}"
}

bounded_timeout() {
    local desired="$1" reserve="$2" limit remaining
    shift 2
    limit="$desired"
    if [[ -n "$KERNEL_DEADLINE" ]]; then
        read_uptime_seconds
        remaining=$((SOFT_DEADLINE - uptime_seconds - reserve))
        ((remaining > 0)) || fail kernel-deadline-exhausted
        ((remaining < limit)) && limit="$remaining"
    fi
    /usr/bin/timeout --kill-after=2 "$limit" "$@"
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

# Asterinas' reboot syscall intentionally jumps straight to the platform
# restart path. Stop every known writer and prove that the desktop user has no
# surviving process before syncing the non-journaled ext2 root. Keep all
# escalation paths bounded inside the kernel's remaining reboot guard.
[[ "$UNIT_DIR" == /* && -d "$UNIT_DIR" && ! -L "$UNIT_DIR" ]] ||
    fail invalid-unit-dir
present_units=()
# Evidence services can run as root while appending persistent browser logs.
# Discover installed ones so future desktop profiles do not evade the UID-1000
# process check simply because their unit name was not listed here.
for unit_path in "$UNIT_DIR"/asterinas-*-evidence.service; do
    [[ -f "$unit_path" ]] && present_units+=("${unit_path##*/}")
done
for unit in "${WRITE_HEAVY_UNITS[@]}"; do
    [[ -f "$UNIT_DIR/$unit" ]] && present_units+=("$unit")
done
emit "ASTERINAS_USERSPACE_REBOOT_QUIESCE_START deadline=$DEADLINE units=${#present_units[@]}"
if ((${#present_units[@]} > 0)); then
    if ! bounded_timeout "$QUIESCE_TIMEOUT_SECONDS" "$SYNC_RESERVE_SECONDS" \
        systemctl stop "${present_units[@]}"; then
        emit "ASTERINAS_USERSPACE_REBOOT_UNIT_STOP state=escalate"
        bounded_timeout "$UNIT_KILL_TIMEOUT_SECONDS" "$SYNC_RESERVE_SECONDS" \
            systemctl kill --kill-who=all --signal=KILL "${present_units[@]}" ||
            fail unit-quiesce
        # A signal request is not proof that a root-owned writer has exited.
        bounded_timeout "$UNIT_KILL_TIMEOUT_SECONDS" "$SYNC_RESERVE_SECONDS" \
            systemctl stop "${present_units[@]}" || fail unit-quiesce
    fi
fi

count_user_processes() {
    local pid process_ids status
    local count=0

    if process_ids="$(pgrep -u "$USER_ID")"; then
        :
    else
        status=$?
        ((status == 1)) || return 2
    fi
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 2
        ((count += 1))
    done <<<"$process_ids"
    printf '%s\n' "$count"
}

wait_for_user_processes() {
    local limit="$1"
    local elapsed=0

    while ((elapsed < limit)); do
        if [[ -n "$KERNEL_DEADLINE" ]]; then
            read_uptime_seconds
            ((uptime_seconds + SYNC_RESERVE_SECONDS < SOFT_DEADLINE)) || return 1
        fi
        process_count="$(count_user_processes)" || fail invalid-process-list
        ((process_count == 0)) && return 0
        sleep 1 || fail process-wait
        ((elapsed += 1))
    done
    process_count="$(count_user_processes)" || fail invalid-process-list
    ((process_count == 0))
}

process_count="$(count_user_processes)" || fail invalid-process-list
emit "ASTERINAS_USERSPACE_REBOOT_PROCESS state=observed count=$process_count"
if ((process_count > 0)); then
    emit "ASTERINAS_USERSPACE_REBOOT_PROCESS state=signal signal=TERM count=$process_count"
    pkill -TERM -u "$USER_ID" || true
    wait_for_user_processes "$TERM_WAIT_SECONDS" || true
fi
if ((process_count > 0)); then
    emit "ASTERINAS_USERSPACE_REBOOT_PROCESS state=signal signal=KILL count=$process_count"
    pkill -KILL -u "$USER_ID" || true
    wait_for_user_processes "$KILL_WAIT_SECONDS" || true
fi
((process_count == 0)) || fail "writers-remain-$process_count"

emit "ASTERINAS_USERSPACE_REBOOT_QUIESCE_DONE processes=0"
emit "ASTERINAS_USERSPACE_REBOOT_SYNC_START deadline=$DEADLINE"
bounded_timeout "$SYNC_TIMEOUT_SECONDS" 0 sync || fail sync
emit "ASTERINAS_USERSPACE_REBOOT_SYNC_DONE deadline=$DEADLINE"
reboot -f || fail reboot
