#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# USB hotplug gate.
#
# Boots the kernel with QEMU's xHCI controller and a passive init, then asks
# the monitor to add and remove a USB mass-storage device. The gate passes
# when the driver reports controller bring-up and both port transitions.

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: usb_hotplug_check.sh --kernel ELF --workspace DIR [OPTIONS]

Options:
  --kernel PATH     Asterinas x86_64 multiboot2 ELF (required).
  --workspace DIR   Initramfs, console transcript and the stick image.
  --timeout SECS    Deadline for bring-up and both transitions (default: 120).
  --accel MODE      'kvm' or 'tcg' (default: kvm).
  --qemu PATH       QEMU binary (default: qemu-system-x86_64).
USAGE
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

KERNEL=""
WORKSPACE=""
TIMEOUT=120
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --workspace | --timeout | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --accel) ACCEL="$2" ;;
                --qemu) QEMU="$2" ;;
            esac
            shift 2
            ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ -n "$KERNEL" && -n "$WORKSPACE" ]] || die_usage "--kernel and --workspace are required"
[[ -f "$KERNEL" ]] || die_usage "the kernel image does not exist: $KERNEL"
[[ "$ACCEL" == "kvm" || "$ACCEL" == "tcg" ]] || die_usage "--accel must be 'kvm' or 'tcg'"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

mkdir -p -- "$WORKSPACE"
CONSOLE="$WORKSPACE/console.txt"
SERIAL_SOCK="$WORKSPACE/serial.sock"
MONITOR_SOCK="$WORKSPACE/monitor.sock"
STICK="$WORKSPACE/usbstick.img"
QEMU_LOG="$WORKSPACE/qemu.log"
rm -f -- "$CONSOLE" "$SERIAL_SOCK" "$MONITOR_SOCK" "$STICK"
truncate -s 16M "$STICK"

INITRAMFS="$(tools/drm/build_probe.sh --arch amd64 \
    --source "$REPO_ROOT/tools/usb/usb_probe.c" --output "$WORKSPACE/probe")"

if [[ "$ACCEL" == "kvm" ]]; then
    ACCEL_ARGS=(-enable-kvm -cpu host)
else
    ACCEL_ARGS=(-cpu Icelake-Server)
fi

"$QEMU" \
    "${ACCEL_ARGS[@]}" \
    -smp 1 -m 2G --no-reboot \
    -display none \
    -chardev "file,id=console,path=$CONSOLE" \
    -device virtio-serial-pci \
    -device virtconsole,chardev=console \
    -device virtio-gpu-pci -vga none \
    -device qemu-xhci \
    -drive "if=none,id=usbstick,format=raw,file=$STICK" \
    -serial "unix:$SERIAL_SOCK,server=on,wait=off" \
    -monitor "unix:$MONITOR_SOCK,server=on,wait=off" \
    -kernel "$KERNEL" \
    -initrd "$INITRAMFS" \
    -append "console=hvc0 loglevel=7 init=/init" \
    >"$QEMU_LOG" 2>&1 &
QEMU_PID=$!

cleanup() {
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
}
trap cleanup EXIT

python3 - "$CONSOLE" "$MONITOR_SOCK" "$TIMEOUT" <<'PY'
import os
import socket
import sys
import time

console_path, monitor_path, deadline_seconds = sys.argv[1:4]
deadline = time.time() + float(deadline_seconds)

monitor = None
end = time.time() + 30
while time.time() < end and monitor is None:
    try:
        monitor = socket.socket(socket.AF_UNIX)
        monitor.connect(monitor_path)
    except OSError:
        monitor = None
        time.sleep(0.05)
if monitor is None:
    print('USB_CLIENT no-monitor', flush=True)
    sys.exit(2)


def console_text():
    try:
        with open(console_path, 'rb') as handle:
            return handle.read()
    except FileNotFoundError:
        return b''


added = removed = disconnected = False
while time.time() < deadline:
    text = console_text()
    if not added and b'controller 1b36:000d' in text and b'ports=' in text:
        time.sleep(1)
        monitor.sendall(b'device_add usb-storage,drive=usbstick,id=stick\n')
        added = True
    if added and not removed and b'port 1 connected' in text:
        time.sleep(1)
        monitor.sendall(b'device_del stick\n')
        removed = True
    if removed and b'port 1 disconnected' in text:
        disconnected = True
        break
    time.sleep(0.2)

print('USB_CLIENT added=%s removed=%s disconnected=%s'
      % (added, removed, disconnected), flush=True)
sys.exit(0 if disconnected else 1)
PY
CLIENT_STATUS=$?
if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (the port transitions did not complete; see %s)\n' "$CONSOLE"
    exit 1
fi

printf 'PASS (xHCI controller up; device add/remove reported on its port)\n'
