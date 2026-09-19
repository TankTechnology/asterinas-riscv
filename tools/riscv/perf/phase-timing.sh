#!/usr/bin/env bash
# Break a desktop boot into phases, timed from the moment QEMU starts.
#
# The evidence script's milestones are guest uptimes, and a guest uptime cannot
# see the time before the guest kernel existed.  That is exactly where a
# comparison between two kernels can hide: Asterinas reports `basic.target` at
# 4 s and all five desktop clients at 11 s, both ahead of the Linux control,
# while the wall clock to the same point is no better.  The difference is the
# bootloader and the stage-1 initramfs, which run before either guest's clock
# starts.
#
# Timestamps come from polling the live log, but with one read per iteration:
# an earlier version ran a separate `sudo grep` for every pattern on every poll,
# which took long enough per iteration to report two phases five seconds apart
# as simultaneous.  A `tail -F` per-line timestamp was tried as well and is
# worse here -- QEMU's stdout is block-buffered when it is a file rather than a
# tty, so lines arrive in 4 KiB bursts and their arrival times say nothing about
# when they were produced.  Polling a file that the gate writes and flushes
# itself is the measurement that actually reflects the guest.
#
# Usage: phase-timing.sh <run-name> [make vars...]
set -uo pipefail

readonly NAME="${1:?usage: phase-timing.sh <run-name> [make vars...]}"
shift

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly OUT="$ROOT/target/debian-riscv/desktop-drm/$NAME"
readonly LIVE="$OUT/desktop-drm.live.log"
readonly CAPTURE="$ROOT/target/phase-timing-$NAME.txt"

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
    printf '%s ERROR: a QEMU is already running; refusing to measure\n' "$NAME" >&2
    exit 1
fi

: >"$CAPTURE"

sudo -E env "PATH=$PATH" make -C "$ROOT" test_riscv_debian_desktop_drm_gate \
    DEBIAN_DRM_GATE_OUTPUT="$OUT" "$@" \
    >"/tmp/gate-$NAME.log" 2>&1 &
gate_pid=$!

qemu_pid=""
for _ in $(seq 1 900); do
    qemu_pid="$(live_qemus | head -1)"
    [[ -n "$qemu_pid" ]] && break
    kill -0 "$gate_pid" 2>/dev/null || break
    sleep 0.2
done
if [[ -z "$qemu_pid" ]]; then
    printf '%s ERROR: no QEMU process appeared\n' "$NAME" >&2
    exit 1
fi

read -r start_ticks < <(sed 's/.*) //' "/proc/$qemu_pid/stat" 2>/dev/null | awk '{print $20}')
readonly HZ="$(getconf CLK_TCK)"
readonly BOOT_TIME="$(awk '/^btime/{print $2}' /proc/stat)"
qemu_start=$(( BOOT_TIME + ${start_ticks:-0} / HZ ))

# There is deliberately no per-component phase here. The evidence script
# reports Xorg's arrival only inside the `LAUNCH` line, which it emits once
# *all* five components are up, so a pattern like `xorg=[0-9]` matches that
# line and reports Xorg as starting at the same moment as xterm. It looks like
# data and is not -- timing a component needs its own marker first.
readonly -a PHASES=(
    "opensbi|OpenSBI"
    "u-boot|U-Boot"
    "stage1|DEBIAN_STAGE1_PROGRESS"
    "kernel-handoff|handoff-enter"
    "systemd|Reached target"
    "basic.target|DEBIAN_DESKTOP_DRM_BOOT phase=basic-target"
    "clients|DEBIAN_DESKTOP_DRM_LAUNCH"
    "ready|DEBIAN_DESKTOP_DRM_READY"
)

declare -A seen_at=()
deadline=$(( SECONDS + 900 ))
while (( SECONDS < deadline )); do
    # One read per poll; every pattern is matched against the same snapshot.
    snapshot="$(sudo cat "$LIVE" 2>/dev/null)"
    if [[ -n "$snapshot" ]]; then
        for entry in "${PHASES[@]}"; do
            name="${entry%%|*}"; pattern="${entry#*|}"
            [[ -n "${seen_at[$name]:-}" ]] && continue
            if grep -aqE "$pattern" <<<"$snapshot"; then
                seen_at[$name]="$(date +%s.%N)"
            fi
        done
    fi
    [[ -n "${seen_at[ready]:-}" ]] && break
    [[ -z "$(live_qemus)" ]] && break
    sleep 0.1
done
sleep 0.5

printf '\n=== %s phase timings (seconds since QEMU start) ===\n' "$NAME"
for entry in "${PHASES[@]}"; do
    name="${entry%%|*}"
    if [[ -z "${seen_at[$name]:-}" ]]; then
        printf '  %6s     %s\n' "-" "$name"
        continue
    fi
    printf '  %6.2f s  %s\n' \
        "$(echo "${seen_at[$name]} - $qemu_start" | bc -l)" "$name"
done

wait "$gate_pid" 2>/dev/null
