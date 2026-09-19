#!/usr/bin/env bash
# Time a desktop-DRM gate run from the QEMU process's real start to the READY
# marker, so runs can be compared across configurations.
#
# The gate itself records no elapsed time, and timing from a wrapper's own start
# is wrong: the gate copies a 1 GiB root image before it launches QEMU, and that
# copy is not boot time. Reading the QEMU process's start out of /proc is
# exact and independent of anything the wrapper does.
#
# Usage: measure-desktop.sh <run-name> [extra make vars...]
set -uo pipefail

readonly NAME="${1:?usage: measure-desktop.sh <run-name> [make vars...]}"
shift

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly OUT="$ROOT/target/debian-riscv/desktop-drm/$NAME"
readonly LIVE="$OUT/desktop-drm.live.log"

# A stale QEMU from an earlier run would be mistaken for this run's, and would
# also steal CPU from the measurement. Refuse to start rather than report a
# number that is quietly wrong.
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
    printf '%s ERROR: a QEMU is already running (%s); refusing to measure\n' \
        "$NAME" "$(live_qemus | tr '\n' ' ')" >&2
    exit 1
fi

# Refuse to report a number from a debug kernel.
#
# `Makefile` defaults RELEASE=0, so a plain `make kernel` builds an unoptimized
# image that is 30-90x slower per syscall than the release build. Measuring one
# of those and calling it "the kernel's performance" is exactly the mistake
# this tooling was written to stop repeating, and the difference shows up only
# as an unexplained large constant, which is easy to misread as an architectural
# problem. Set ASTERINAS_ALLOW_DEBUG_KERNEL=1 to override deliberately.
readonly KERNEL_IMAGE="$ROOT/target/osdk/aster-kernel-osdk-bin.Image"
if [[ -f "$KERNEL_IMAGE" && -z "${ASTERINAS_ALLOW_DEBUG_KERNEL:-}" ]]; then
    image_bytes="$(stat -c %s "$KERNEL_IMAGE")"
    # A release build is ~6 MB and a debug build ~16 MB; the gap is wide enough
    # that the size is a reliable signal without inspecting the build profile.
    if (( image_bytes > 10000000 )); then
        printf '%s ERROR: %s is %s bytes, which is an unoptimized debug build\n' \
            "$NAME" "$KERNEL_IMAGE" "$image_bytes" >&2
        printf '  Build it with RELEASE=1, or set ASTERINAS_ALLOW_DEBUG_KERNEL=1\n' >&2
        printf '  to measure it anyway.\n' >&2
        exit 1
    fi
fi

# Optional extra kernel command line, for runs that enable a kernel-side
# profiler.  It goes through the environment rather than a make variable
# because the gate reads ASTERINAS_DESKTOP_DRM_BOOTARGS from os.environ;
# passing it as a make variable silently does nothing and the run comes back
# unprofiled.
bootargs_env=()
if [[ -n "${ASTERINAS_PERF_BOOTARGS:-}" ]]; then
    bootargs_env=("ASTERINAS_DESKTOP_DRM_BOOTARGS=$ASTERINAS_PERF_BOOTARGS")
    printf '%s: kernel cmdline override: %s\n' "$NAME" "$ASTERINAS_PERF_BOOTARGS"
fi

# Start the gate in the background and wait for its QEMU to appear.
# The gate refuses to run as a non-root user, and -E keeps the caller's
# environment (PATH included) so make is found under sudo.
sudo -E env "PATH=$PATH" "${bootargs_env[@]}" \
    make -C "$ROOT" test_riscv_debian_desktop_drm_gate \
    DEBIAN_DRM_GATE_OUTPUT="$OUT" "$@" \
    >"/tmp/gate-$NAME.log" 2>&1 &
gate_pid=$!

qemu_pid=""
for _ in $(seq 1 600); do
    qemu_pid="$(live_qemus | head -1)"
    [[ -n "$qemu_pid" ]] && break
    kill -0 "$gate_pid" 2>/dev/null || break
    sleep 1
done

if [[ -z "$qemu_pid" ]]; then
    printf '%s ERROR: no QEMU process appeared\n' "$NAME"
    wait "$gate_pid"
    exit 1
fi

# Field 22 of /proc/pid/stat is the start time in clock ticks since boot.
# The comm field (2) is parenthesised and may itself contain spaces, so count
# from after the last ')' rather than trusting plain field splitting -- from
# there, starttime is field 20.
read -r start_ticks < <(
    sed 's/.*) //' "/proc/$qemu_pid/stat" 2>/dev/null | awk '{print $20}'
)
readonly HZ="$(getconf CLK_TCK)"
readonly BOOT_TIME="$(awk '/^btime/{print $2}' /proc/stat)"

qemu_start="$BOOT_TIME"
if [[ -n "${start_ticks:-}" ]]; then
    qemu_start=$(( BOOT_TIME + start_ticks / HZ ))
fi
printf '%s: qemu pid %s started at %s\n' "$NAME" "$qemu_pid" \
    "$(date -d "@$qemu_start" +%H:%M:%S)"

# Watch the live log for the terminal marker; stamp when it lands.
#
# Liveness is checked with `ps`, not `kill -0`: QEMU runs as root and this
# script as an ordinary user, and `kill -0` on another user's process fails
# with EPERM, which would look exactly like the process having exited.
qemu_alive() {
    local state
    state="$(ps -o stat= -p "$1" 2>/dev/null)" || return 1
    [[ -n "$state" && "$state" != Z* ]]
}
while qemu_alive "$qemu_pid"; do
    if sudo grep -aqE 'DEBIAN_DESKTOP_DRM_READY|DEBIAN_DESKTOP_DRM_FAIL' "$LIVE" 2>/dev/null; then
        break
    fi
    sleep 2
done

end="$(date +%s)"
elapsed=$(( end - qemu_start ))

marker="$(sudo sh -c "tr -d '\r' < '$LIVE' 2>/dev/null | grep -aoE 'DEBIAN_DESKTOP_DRM_(READY|FAIL)[^\n]*' | tail -1")"
printf '%s: %s\n' "$NAME" "$marker"
printf '%s: ELAPSED %d s  (qemu %s -> marker %s)\n' \
    "$NAME" "$elapsed" "$(date -d "@$qemu_start" +%H:%M:%S)" "$(date -d "@$end" +%H:%M:%S)"

# Let the gate finish so its own teardown and logging complete.
wait "$gate_pid"
