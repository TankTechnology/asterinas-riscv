#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# PRIME / dma-buf gate for both architectures.
#
# Builds `tools/drm/prime_probe.c` as a one-file initramfs `/init` and boots it
# on the x86_64 or RISC-V kernel with a virtio-gpu device attached. The probe
# exports a dumb buffer as a dma-buf descriptor, maps it, imports it into a
# second DRM file, passes it to a child over `SCM_RIGHTS`, and checks the
# negative cases (non-dma-buf fd -> EINVAL, closed fd -> EBADF).
#
# The x86_64 build must be an OSDK vmm-direct ELF, the RISC-V build an Sv39
# `Image`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: prime_check.sh --arch ARCH --kernel PATH --workspace DIR [OPTIONS]

Options:
  --arch ARCH       amd64 or riscv64 (required).
  --kernel PATH     Kernel built for that architecture (required).
  --workspace DIR   Initramfs, transcript and QEMU log.
  --timeout SECS    Deadline for boot and probe (default: 180).
  --loglevel N      Kernel log level (default: 4, Linux numbering).
  --accel MODE      x86_64 only: kvm or tcg (default: kvm).
  --cc PATH         Cross compiler (default: gcc / riscv64-linux-gnu-gcc).
  --qemu PATH       QEMU binary (default: qemu-system-<arch>).

Exit status is 0 only if the probe prints `PRIMEPROBE_PASS`.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

ARCH=""
KERNEL=""
WORKSPACE=""
TIMEOUT=180
LOGLEVEL=4
ACCEL="kvm"
CC=""
QEMU=""
QEMU_EXPLICIT=0

while (( $# > 0 )); do
    case "$1" in
        --arch | --kernel | --workspace | --timeout | --loglevel | --accel | \
            --cc | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --arch) ARCH="$2" ;;
                --kernel) KERNEL="$2" ;;
                --workspace) WORKSPACE="$2" ;;
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
[[ -n "$KERNEL" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel and --workspace are required"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$LOGLEVEL" =~ ^[0-9]+$ ]] || die_usage "--loglevel must be a non-negative integer"
[[ "$ACCEL" == "kvm" || "$ACCEL" == "tcg" ]] ||
    die_usage "--accel must be 'kvm' or 'tcg'"
[[ -f "$KERNEL" ]] || die_usage "the kernel image does not exist: $KERNEL"
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
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
BUILD_PROBE="$REPO_ROOT/tools/drm/build_probe.sh"
[[ -x "$BUILD_PROBE" ]] || {
    printf 'error: probe builder is missing: %s\n' "$BUILD_PROBE" >&2
    exit 1
}

mkdir -p -- "$WORKSPACE"
SERIAL="$WORKSPACE/console.txt"
CONSOLE="$WORKSPACE/console.txt"
QEMU_LOG="$WORKSPACE/qemu.log"

BUILD_ARGS=(--arch "$ARCH" --output "$WORKSPACE/probe" --source "$REPO_ROOT/tools/drm/prime_probe.c")
[[ -n "$CC" ]] && BUILD_ARGS+=(--cc "$CC")
INITRAMFS="$("$BUILD_PROBE" "${BUILD_ARGS[@]}")"

rm -f -- "$SERIAL"

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
        -serial "file:$SERIAL" \
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
    if grep -aq 'PRIMEPROBE_PASS' "$CONSOLE" 2>/dev/null; then
        STATUS="pass"
        break
    fi
    if grep -aq 'PRIMEPROBE_FAIL' "$CONSOLE" 2>/dev/null; then
        STATUS="fail"
        break
    fi
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        STATUS="qemu-exited"
        break
    fi
    sleep 1
done

grep -a 'PRIMEPROBE_' "$CONSOLE" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' || true

if [[ "$STATUS" != "pass" ]]; then
    printf 'FAIL (%s; see %s and %s)\n' "$STATUS" "$CONSOLE" "$QEMU_LOG"
    exit 1
fi

printf 'PASS (%s PRIME: export, shared mmap, import, SCM_RIGHTS, negative cases)\n' \
    "$ARCH"
