#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Boots an architecture-neutral probe as a one-file initramfs `/init`.
#
# The probe is expected to print `*_PASS` on success and a `*FAIL*` line on
# failure, exactly like `prime_probe.c`, `sync_probe.c` and `atomic_probe.c`.
# This script builds the probe for the requested architecture, boots it with
# a virtio-gpu device, and prints the probe's own transcript.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: initramfs_probe_check.sh --arch ARCH --kernel PATH --probe SOURCE
                                --workspace DIR [OPTIONS]

Options:
  --arch ARCH       amd64 or riscv64 (required).
  --kernel PATH     Kernel built for that architecture (required).
  --probe PATH      Probe source file with a `/init`-compatible main (required).
  --workspace DIR   Initramfs, transcript and QEMU log.
  --pass MARKER     Success marker to grep for (default: PROBE_PASS, which
                    matches `SYNCPROBE_PASS`, `ATOMICPROBE_PASS`, ...).
  --timeout SECS    Deadline for boot and probe (default: 180).
  --loglevel N      Kernel log level (default: 4, Linux numbering).
  --accel MODE      x86_64 only: kvm or tcg (default: kvm).
  --cc PATH         Cross compiler (default: gcc / riscv64-linux-gnu-gcc).
  --qemu PATH       QEMU binary (default: qemu-system-<arch>).
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

ARCH=""
KERNEL=""
PROBE=""
WORKSPACE=""
PASS_MARKER="PROBE_PASS"
TIMEOUT=180
LOGLEVEL=4
ACCEL="kvm"
CC=""
QEMU=""
QEMU_EXPLICIT=0

while (( $# > 0 )); do
    case "$1" in
        --arch | --kernel | --probe | --workspace | --pass | --timeout | \
            --loglevel | --accel | --cc | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --arch) ARCH="$2" ;;
                --kernel) KERNEL="$2" ;;
                --probe) PROBE="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --pass) PASS_MARKER="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --accel) ACCEL="$2" ;;
                --cc) CC="$2" ;;
                --qemu) QEMU="$2"; QEMU_EXPLICIT=1 ;;
            esac
            shift 2
            ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ "$ARCH" == "amd64" || "$ARCH" == "riscv64" ]] ||
    die_usage "--arch must be 'amd64' or 'riscv64'"
[[ -n "$KERNEL" && -n "$PROBE" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --probe and --workspace are required"
[[ -f "$KERNEL" ]] || die_usage "the kernel image does not exist: $KERNEL"
[[ -f "$PROBE" ]] || die_usage "the probe source does not exist: $PROBE"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$LOGLEVEL" =~ ^[0-9]+$ ]] || die_usage "--loglevel must be a non-negative integer"
[[ "$ACCEL" == "kvm" || "$ACCEL" == "tcg" ]] ||
    die_usage "--accel must be 'kvm' or 'tcg'"

if [[ $QEMU_EXPLICIT -eq 0 ]]; then
    case "$ARCH" in
        amd64) QEMU="qemu-system-x86_64" ;;
        riscv64) QEMU="qemu-system-riscv64" ;;
    esac
fi
for tool in python3 timeout cpio "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_PROBE="$SCRIPT_DIR/build_probe.sh"
[[ -x "$BUILD_PROBE" ]] || {
    printf 'error: probe builder is missing: %s\n' "$BUILD_PROBE" >&2
    exit 1
}

mkdir -p -- "$WORKSPACE"
CONSOLE="$WORKSPACE/console.txt"
QEMU_LOG="$WORKSPACE/qemu.log"

BUILD_ARGS=(--arch "$ARCH" --output "$WORKSPACE/probe" --source "$PROBE")
[[ -n "$CC" ]] && BUILD_ARGS+=(--cc "$CC")
INITRAMFS="$("$BUILD_PROBE" "${BUILD_ARGS[@]}")"

rm -f -- "$CONSOLE"

if [[ "$ARCH" == "riscv64" ]]; then
    "$QEMU" \
        -M virt \
        -cpu rv64,svpbmt=true,zkr=true,sv48=false,svadu=false,svade=true \
        -m 2G \
        -smp 1 \
        -no-reboot \
        -kernel "$KERNEL" \
        -initrd "$INITRAMFS" \
        -append "console=ttyS0 loglevel=$LOGLEVEL" \
        -device virtio-gpu-device \
        -serial "file:$CONSOLE" \
        -display none \
        >"$QEMU_LOG" 2>&1 &
else
    [[ "$ACCEL" == "kvm" ]] && ACCEL_ARGS=(-enable-kvm -cpu host) ||
        ACCEL_ARGS=(-cpu Icelake-Server)
    "$QEMU" \
        "${ACCEL_ARGS[@]}" \
        -smp 1 \
        -m 2G \
        --no-reboot \
        -display none \
        -chardev "file,id=console,path=$CONSOLE" \
        -device virtio-serial-pci \
        -device virtconsole,chardev=console \
        -device virtio-gpu-pci \
        -vga none \
        -kernel "$KERNEL" \
        -initrd "$INITRAMFS" \
        -append "console=hvc0 loglevel=$LOGLEVEL init=/init" \
        >"$QEMU_LOG" 2>&1 &
fi
QEMU_PID=$!

cleanup() {
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
}
trap cleanup EXIT

STATUS="timeout"
for _ in $(seq 1 "$TIMEOUT"); do
    if grep -aq "$PASS_MARKER" "$CONSOLE" 2>/dev/null; then
        STATUS="pass"
        break
    fi
    if grep -aqE 'PROBE_.*FAIL|Uncaught panic|Kernel panic' "$CONSOLE" 2>/dev/null; then
        STATUS="fail"
        break
    fi
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        STATUS="qemu-exited"
        break
    fi
    sleep 1
done

grep -a 'PROBE_' "$CONSOLE" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' || true

if [[ "$STATUS" != "pass" ]]; then
    printf 'FAIL (%s; see %s and %s)\n' "$STATUS" "$CONSOLE" "$QEMU_LOG"
    exit 1
fi

printf 'PASS (%s %s)\n' "$ARCH" "$(basename "${PROBE%.c}")"
