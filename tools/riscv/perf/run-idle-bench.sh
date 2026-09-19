#!/usr/bin/env bash
# Run the in-guest microbenchmark with the desktop session masked, so the
# numbers describe an otherwise-idle kernel.
#
# The bench is emitted before the evidence script starts polling for the
# desktop, so the run is stopped as soon as BENCH_DONE appears rather than
# waiting out the desktop deadline that can never be met with the session
# masked.
#
# Usage: run-idle-bench.sh <run-name>
set -uo pipefail

readonly NAME="${1:?usage: run-idle-bench.sh <run-name>}"
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
readonly HERE="$(dirname "${BASH_SOURCE[0]}")"
readonly DERIVED="$ROOT/target/drm-mesa/rootfs-idle-bench"
readonly OUT="$ROOT/target/debian-riscv/desktop-drm/$NAME"
readonly LIVE="$OUT/desktop-drm.live.log"

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

"$HERE/toggle-desktop-session.sh" on

sudo rm -rf "$DERIVED"
sudo python3 -m tools.riscv.debian.rootfs.dev_overlay materialize \
    --base-dir "$ROOT/target/debian-riscv/desktop-drm/rootfs" \
    --spec "$ROOT/target/drm-mesa/spec-bench.json" \
    --output-dir "$DERIVED" >/dev/null || {
    printf '%s ERROR: materialize failed\n' "$NAME" >&2
    exit 1
}

# `asterinas.boot_bench=1` is what makes the evidence script run the bench.
# Only the kernel's own arguments go before the `--`.
#
# This has to go through the *environment*: the gate reads
# ASTERINAS_DESKTOP_DRM_BOOTARGS from os.environ. Setting a make variable of a
# similar name does nothing, which is how an earlier version of this script
# produced a run with no BENCH lines at all -- the guest booted on the default
# command line and the bench never started.
readonly BOOTARGS="console=ttyS0 loglevel=4 asterinas.boot_bench=1 init=/init -- --root-init=systemd"

sudo -E env "PATH=$PATH" "ASTERINAS_DESKTOP_DRM_BOOTARGS=$BOOTARGS" \
    make -C "$ROOT" test_riscv_debian_desktop_drm_gate \
    DEBIAN_DRM_GATE_OUTPUT="$OUT" \
    "DEBIAN_DRM_ROOT_IMAGE=$DERIVED/debian-root.ext2" \
    "DEBIAN_DRM_ROOT_MANIFEST=$DERIVED/rootfs-manifest.json" \
    DEBIAN_DRM_GRAPHICS_DEVICE=virtio-gpu-device \
    DEBIAN_DESKTOP_BOOT_TIMEOUT=1800 \
    >"/tmp/gate-$NAME.log" 2>&1 &
gate_pid=$!

# Wait for the bench to finish, then stop the guest: with the session masked
# the desktop can never come up, so the gate would otherwise sit until its
# deadline.
deadline=$(( SECONDS + 1500 ))
while (( SECONDS < deadline )); do
    if sudo grep -aq 'DEBIAN_DESKTOP_DRM_BENCH_DONE' "$LIVE" 2>/dev/null; then
        printf '%s: bench finished at %s\n' "$NAME" "$(date +%H:%M:%S)"
        break
    fi
    kill -0 "$gate_pid" 2>/dev/null || break
    sleep 5
done

sleep 5
for p in $(live_qemus); do sudo kill -9 "$p" 2>/dev/null; done
wait "$gate_pid" 2>/dev/null

printf '\n=== %s bench numbers ===\n' "$NAME"
sudo sh -c "tr -d '\r' < '$LIVE' 2>/dev/null | grep -aE 'BENCH [a-z]|BENCH begin|BENCH end'"

"$HERE/toggle-desktop-session.sh" off
