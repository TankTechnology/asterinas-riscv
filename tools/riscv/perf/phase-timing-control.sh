#!/usr/bin/env bash
# Phase timings for the Linux control, matched to phase-timing.sh so the two
# tables can be read side by side.
#
# The control boots differently: QEMU loads the Debian kernel directly with
# `-kernel`, so there is no U-Boot and no stage-1 initramfs.  That is the point
# of measuring it -- those two phases run before either guest's uptime clock
# starts, and they are invisible to the evidence script's milestones.
#
# Usage: phase-timing-control.sh <tag> [timeout-seconds]
set -uo pipefail

readonly TAG="${1:?usage: phase-timing-control.sh <tag> [timeout]}"
readonly TIMEOUT="${2:-600}"
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly LOG="/tmp/linux-control-${TAG}.log"

live_qemus() {
    local p
    for p in $(pgrep -f 'qemu-syste[m]' 2>/dev/null); do
        case "$(ps -o stat= -p "$p" 2>/dev/null)" in
            Z*|"") continue ;;
        esac
        printf '%s\n' "$p"
    done
}
if [[ -n "$(live_qemus)" ]]; then
    printf '%s ERROR: a QEMU is already running; refusing to measure\n' "$TAG" >&2
    exit 1
fi

"$ROOT/target/linux-control/run-control.sh" "$TAG" "$TIMEOUT" >/dev/null 2>&1 &
control_pid=$!

qemu_pid=""
for _ in $(seq 1 900); do
    qemu_pid="$(live_qemus | head -1)"
    [[ -n "$qemu_pid" ]] && break
    kill -0 "$control_pid" 2>/dev/null || break
    sleep 1
done
if [[ -z "$qemu_pid" ]]; then
    printf '%s ERROR: no QEMU process appeared (see %s)\n' "$TAG" "$LOG" >&2
    exit 1
fi

read -r start_ticks < <(sed 's/.*) //' "/proc/$qemu_pid/stat" 2>/dev/null | awk '{print $20}')
readonly HZ="$(getconf CLK_TCK)"
readonly BOOT_TIME="$(awk '/^btime/{print $2}' /proc/stat)"
qemu_start=$(( BOOT_TIME + ${start_ticks:-0} / HZ ))

readonly -a PHASES=(
    "kernel-start|Linux version"
    "systemd|Reached target"
    "basic.target|DEBIAN_DESKTOP_DRM_BOOT phase=basic-target"
    "xorg|xorg=[0-9]"
    "clients|DEBIAN_DESKTOP_DRM_LAUNCH"
    "ready|DEBIAN_DESKTOP_DRM_READY"
)

declare -A seen_at=()
deadline=$(( SECONDS + TIMEOUT + 60 ))
while (( SECONDS < deadline )); do
    for entry in "${PHASES[@]}"; do
        name="${entry%%|*}"; pattern="${entry#*|}"
        [[ -n "${seen_at[$name]:-}" ]] && continue
        if grep -aqE "$pattern" "$LOG" 2>/dev/null; then
            seen_at[$name]=$(( $(date +%s) - qemu_start ))
        fi
    done
    [[ -n "${seen_at[ready]:-}" ]] && break
    [[ -z "$(live_qemus)" ]] && break
    sleep 0.2
done

printf '\n=== %s phase timings (seconds since QEMU start) ===\n' "$TAG"
for entry in "${PHASES[@]}"; do
    name="${entry%%|*}"
    printf '  %6s s  %s\n' "${seen_at[$name]:-?}" "$name"
done

wait "$control_pid" 2>/dev/null
