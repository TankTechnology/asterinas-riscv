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
readonly DESKTOP_READY_FILE="${ASTERINAS_SAFE_REBOOT_DESKTOP_READY_FILE:-/run/asterinas-desktop-ready}"
readonly BOOT_ID_FILE="${ASTERINAS_SAFE_REBOOT_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}"
readonly WATCHDOG_FILE="${ASTERINAS_SAFE_REBOOT_WATCHDOG_FILE:-/proc/sys/kernel/asterinas_reboot_watchdog}"
readonly SYSRQ_FILE="${ASTERINAS_SAFE_REBOOT_SYSRQ_FILE:-/proc/sysrq-trigger}"
readonly DRILL_FILE="${ASTERINAS_SAFE_REBOOT_DRILL_FILE:-/proc/sys/kernel/asterinas_watchdog_drill}"
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

# Past the deadline the board is supposed to be restarting. If the clean path
# cannot finish -- a unit refuses to stop, a sync hangs, the reboot call
# itself fails -- the alternative is a board that stays up with no watchdog:
# the kernel timer was disarmed when this service started, and the desktop
# never published readiness. A hard restart loses at most the writes since the
# last sync and is the only outcome that brings the board back on its own.
force_reboot() {
    local reason="$1"

    emit "ASTERINAS_USERSPACE_REBOOT_FORCED reason=$reason"
    # Ask for the restart without touching the filesystem first. `reboot(2)` in
    # Asterinas syncs before it resets, so it lands on the very wait this path
    # exists to escape; `/proc/sysrq-trigger` deliberately skips that.
    if [[ -w $SYSRQ_FILE ]]; then
        if printf 'b' >"$SYSRQ_FILE" 2>/dev/null; then
            exit 0
        fi
        emit "ASTERINAS_USERSPACE_REBOOT_SYSRQ_FAILED reason=$reason"
    else
        emit "ASTERINAS_USERSPACE_REBOOT_SYSRQ_ABSENT reason=$reason"
    fi
    # Next, stop feeding the hardware watchdog: on a board whose handoff
    # armed one, the SoC resets on its own within the watchdog timeout, with
    # no filesystem work at all. The drill is one-way, so read it first and
    # say so when there is nothing feeding -- a board with no watchdog must
    # not be left waiting for a reset that cannot come.
    local drill=$DRILL_FILE
    if [[ -r $drill && -w $drill ]]; then
        local kicking
        kicking=$(cat "$drill" 2>/dev/null || true)
        if [[ "$kicking" == 1 ]]; then
            if printf '1' >"$drill" 2>/dev/null; then
                emit "ASTERINAS_USERSPACE_REBOOT_WATCHDOG_ARMED reason=$reason"
                # The counter is at most about ten seconds; give the reset
                # room and then keep escalating.
                sleep "$KERNEL_DEADLINE_RESERVE_SECONDS" || true
            fi
        else
            emit "ASTERINAS_USERSPACE_REBOOT_WATCHDOG_UNAVAILABLE reason=$reason value=${kicking:-unreadable}"
        fi
    else
        emit "ASTERINAS_USERSPACE_REBOOT_WATCHDOG_UNAVAILABLE reason=$reason value=no-file"
    fi
    if reboot -f; then
        exit 0
    fi
    fail "forced-reboot-failed-$reason"
}

[[ "$TERM_WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-term-wait
[[ "$KILL_WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-kill-wait
readonly MANUAL_REBOOT="${ASTERINAS_SAFE_REBOOT_MANUAL:-0}"
[[ "$MANUAL_REBOOT" == 0 || "$MANUAL_REBOOT" == 1 ]] || fail invalid-manual-reboot
if [[ "$MANUAL_REBOOT" == 1 && -z "${ASTERINAS_SAFE_REBOOT_AFTER:-}" ]]; then
    fail manual-deadline-required
fi

DEADLINE="${ASTERINAS_SAFE_REBOOT_AFTER:-}"
# An explicit zero disables the userspace self-reboot.  An unset value
# keeps the bounded kernel-cmdline handoff; the board-session default
# supplies no deadline, so a normal boot remains off by default.
[[ "$MANUAL_REBOOT" == 1 && "$DEADLINE" == 0 ]] && fail manual-deadline-required
[[ "$DEADLINE" == 0 ]] && exit 0
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
    # The userspace watchdog reboots `KERNEL_REBOOT_GUARD_SECONDS` before the
    # kernel's hard deadline so the reboot can be clean. A deadline that is
    # not longer than that guard leaves no room: deriving
    # `deadline - guard` would collapse to an immediate reboot, which aborts
    # the boot before the desktop can report ready (the 2026-11-11 S2 boot
    # did exactly this). Refuse up front and leave the short deadline to the
    # kernel's own grace path; the reason doubles as the existing
    # budget-exhausted contract.
    if ((KERNEL_DEADLINE <= KERNEL_REBOOT_GUARD_SECONDS)); then
        fail kernel-deadline-exhausted
    fi
    DEADLINE=$((KERNEL_DEADLINE - KERNEL_REBOOT_GUARD_SECONDS))
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

desktop_handoff_ready() {
    local marker_boot current_boot watchdog_state

    # /run is boot-local and root-owned.  Bind the marker to this exact boot
    # and require the kernel watchdog to have been disarmed by desktop readiness.
    [[ "$DESKTOP_READY_FILE" == /* && -f "$DESKTOP_READY_FILE" &&
       ! -L "$DESKTOP_READY_FILE" && -O "$DESKTOP_READY_FILE" &&
       "$BOOT_ID_FILE" == /* && -f "$BOOT_ID_FILE" && ! -L "$BOOT_ID_FILE" &&
       "$WATCHDOG_FILE" == /* && -f "$WATCHDOG_FILE" && ! -L "$WATCHDOG_FILE" ]] ||
        return 1
    read -r marker_boot <"$DESKTOP_READY_FILE" || return 1
    read -r current_boot <"$BOOT_ID_FILE" || return 1
    read -r watchdog_state <"$WATCHDOG_FILE" || return 1
    [[ "$marker_boot" =~ ^[0-9a-f-]{36}$ &&
       "$marker_boot" == "$current_boot" && "$watchdog_state" == 0 ]]
}

bounded_timeout() {
    local desired="$1" reserve="$2" limit remaining
    shift 2
    limit="$desired"
    if [[ -n "$KERNEL_DEADLINE" && "$MANUAL_REBOOT" == 0 ]]; then
        read_uptime_seconds
        remaining=$((SOFT_DEADLINE - uptime_seconds - reserve))
        ((remaining > 0)) || fail kernel-deadline-exhausted
        ((remaining < limit)) && limit="$remaining"
    fi
    /usr/bin/timeout --kill-after=2 "$limit" "$@"
}

uptime_seconds=0
read_uptime_seconds
if [[ "$MANUAL_REBOOT" == 1 ]] && ! desktop_handoff_ready; then
    fail manual-reboot-not-ready
fi

emit "ASTERINAS_USERSPACE_REBOOT_ARMED uptime=$uptime_seconds deadline=$DEADLINE"
while ((uptime_seconds < DEADLINE)); do
    if [[ "$MANUAL_REBOOT" == 0 ]] && desktop_handoff_ready; then
        emit "ASTERINAS_USERSPACE_REBOOT_DISARMED reason=desktop-ready"
        exit 0
    fi
    remaining=$((DEADLINE - uptime_seconds))
    sleep_seconds=$((remaining < MAX_SLEEP_SECONDS ? remaining : MAX_SLEEP_SECONDS))
    sleep "$sleep_seconds" || fail sleep
    previous_uptime_seconds=$uptime_seconds
    read_uptime_seconds
    ((uptime_seconds >= previous_uptime_seconds)) || fail uptime-regressed
done
if [[ "$MANUAL_REBOOT" == 0 ]] && desktop_handoff_ready; then
    emit "ASTERINAS_USERSPACE_REBOOT_DISARMED reason=desktop-ready"
    exit 0
fi

# Asterinas' reboot syscall intentionally jumps straight to the platform
# restart path. Stop every known writer and prove that the desktop user has no
# surviving process before syncing the non-journaled ext2 root. Keep all
# escalation paths bounded inside the kernel's remaining reboot guard, unless
# the current desktop boot has already disarmed that watchdog and root has
# explicitly requested a manual safe reboot.
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
        emit "ASTERINAS_USERSPACE_REBOOT_UNIT_STOP state=escalate units=${present_units[*]}"
        bounded_timeout "$UNIT_KILL_TIMEOUT_SECONDS" "$SYNC_RESERVE_SECONDS" \
            systemctl kill --kill-who=all --signal=KILL "${present_units[@]}" ||
            emit "ASTERINAS_USERSPACE_REBOOT_UNIT_STOP state=kill-failed units=${present_units[*]}"
        # A signal request is not proof that a root-owned writer has exited.
        bounded_timeout "$UNIT_KILL_TIMEOUT_SECONDS" "$SYNC_RESERVE_SECONDS" \
            systemctl stop "${present_units[@]}" || force_reboot unit-quiesce
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
        if [[ -n "$KERNEL_DEADLINE" && "$MANUAL_REBOOT" == 0 ]]; then
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
((process_count == 0)) || force_reboot "writers-remain-$process_count"

emit "ASTERINAS_USERSPACE_REBOOT_QUIESCE_DONE processes=0"
emit "ASTERINAS_USERSPACE_REBOOT_SYNC_START deadline=$DEADLINE"
bounded_timeout "$SYNC_TIMEOUT_SECONDS" 0 sync || force_reboot sync
emit "ASTERINAS_USERSPACE_REBOOT_SYNC_DONE deadline=$DEADLINE"
reboot -f || force_reboot reboot-call
