#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# USB hotplug gate.
#
# Boots the kernel with QEMU's xHCI controller and the enumeration probe, then
# asks the monitor to add and remove a USB mass-storage device. The gate passes
# when the driver reports the controller, the port transition, an enumerated
# device under /sys/bus/usb/devices with a vendor/product id, and the removal.

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: usb_hotplug_check.sh --kernel ELF --workspace DIR [OPTIONS]

Options:
  --kernel PATH     Asterinas x86_64 multiboot2 ELF (required).
  --workspace DIR   Initramfs, console transcript and the stick image.
  --timeout SECS    Deadline for bring-up, enumeration and removal (default: 180).
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
TIMEOUT=180
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
USB_ROOT="$WORKSPACE/usbstick-root"
rm -rf -- "$USB_ROOT"
mkdir -p -- "$USB_ROOT"
printf 'ASTERINAS-USB-OK\n' >"$USB_ROOT/hello.txt"
truncate -s 16M "$STICK"
# The kernel ext2 driver only mounts 4 KiB blocks.
mke2fs -q -F -t ext2 -b 4096 -O ^ext_attr,^resize_inode,^dir_index -d "$USB_ROOT" "$STICK"
printf 'ASTERUSB' | dd of="$STICK" bs=1 conv=notrunc status=none

INITRAMFS="$(tools/drm/build_probe.sh --arch amd64 \
    --source "$REPO_ROOT/tools/usb/usb_probe.c" --output "$WORKSPACE/probe")"

if [[ "$ACCEL" == "kvm" ]]; then
    ACCEL_ARGS=(-enable-kvm -cpu host)
else
    ACCEL_ARGS=(-cpu Icelake-Server)
fi

TRACE_ARGS=()
if [[ -n "${QEMU_TRACE:-}" ]]; then
    IFS=',' read -r -a trace_patterns <<<"$QEMU_TRACE"
    for pattern in "${trace_patterns[@]}"; do
        TRACE_ARGS+=(-trace "$pattern")
    done
    TRACE_ARGS+=(-D "$WORKSPACE/qemu-trace.log")
fi

"$QEMU" \
    "${ACCEL_ARGS[@]}" \
    "${TRACE_ARGS[@]}" \
    -smp 1 -m 2G --no-reboot \
    -display none \
    -chardev "file,id=console,path=$CONSOLE" \
    -device virtio-serial-pci \
    -device virtconsole,chardev=console \
    -device virtio-gpu-pci -vga none \
    -device "${QEMU_XHCI_DEV:-qemu-xhci}" \
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


CONFIG_MARKERS = [
    b'config 1: interfaces=1 selected',
    b'interface 0: class=0x08',
    b'endpoint 81:bulk in',
    b'endpoint 02:bulk out',
    b'storage on slot 1: vendor="QEMU"',
    b'blocks=32768 block_size=512',
    b'registered /dev/sda: 32768 sectors',
    b'USBPROBE_BLOCK dev=/dev/sda read=512 magic=4153544552',
    b'USBPROBE_MOUNT rc=0',
    b'USBPROBE_MOUNT_READ n=17 text=ASTERINAS-USB-OK',
]

added = removed = disconnected = enumerated = configured = False
device_line = ''
while time.time() < deadline:
    text = console_text()
    if not added and b'controller 1b36:000d' in text and b'ports=' in text:
        time.sleep(1)
        monitor.sendall(b'device_add usb-storage,drive=usbstick,id=stick\n')
        added = True
    if added and not enumerated:
        for line in text.splitlines():
            if line.startswith(b'USBPROBE_DEVICE '):
                device_line = line.decode(errors='replace')
                enumerated = True
            elif line.startswith(b'USBPROBE_CONFIG '):
                print(line.decode(errors='replace'), flush=True)
    configured = all(marker in text for marker in CONFIG_MARKERS)
    if enumerated and configured and not removed:
        time.sleep(1)
        monitor.sendall(b'device_del stick\n')
        removed = True
    if removed and b'port 1 disconnected' in text:
        disconnected = True
        break
    time.sleep(0.2)

print('USB_CLIENT added=%s enumerated=%s configured=%s removed=%s disconnected=%s'
      % (added, enumerated, configured, removed, disconnected), flush=True)
if device_line:
    print(device_line, flush=True)
sys.exit(0 if disconnected and enumerated and configured else 1)
PY
CLIENT_STATUS=$?
if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (enumeration, configuration or the port transitions did not complete; see %s)\n' "$CONSOLE"
    exit 1
fi

printf 'PASS (hot-added stick enumerated, mounted read-only, file read, unmounted)\n'
