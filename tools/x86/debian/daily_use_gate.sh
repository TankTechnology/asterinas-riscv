#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Daily-use measurement gate for the x86_64 Debian image.
#
# Boots the image on the Asterinas kernel, logs in over the serial console,
# and runs `daily_use_checklist.sh` inside the guest. The checklist reports one
# line per domain (desktop+DRI3, application, apt, clipboard, input method,
# audio, USB hotplug, suspend/wake); the host answers its two WAIT markers by
# hot-adding a USB stick and waking the guest from suspend.
#
# This gate measures the current state. A `fail` or `missing` domain does not
# fail the run; the gate fails only when the checklist itself did not complete,
# so the matrix it prints is a faithful snapshot.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: daily_use_gate.sh --kernel ELF --initramfs CPIO --base-root IMG
                         --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy for the run (required).
  --workspace DIR     Image, checklist, transcripts and the matrix.
  --timeout SECONDS   Deadline for login, checklist and power-off (default: 900).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).

The guest account is `asterinas` (uid 1000, password `asterinas`, passwordless
sudo). Results land in `<workspace>/matrix.tsv` and on stdout.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

KERNEL=""
INITRAMFS=""
BASE_ROOT=""
WORKSPACE=""
TIMEOUT=900
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --timeout | \
            --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
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

[[ -n "$KERNEL" && -n "$INITRAMFS" && -n "$BASE_ROOT" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --initramfs, --base-root and --workspace are required"
for path in "$KERNEL" "$INITRAMFS" "$BASE_ROOT"; do
    [[ -f "$path" ]] || die_usage "input does not exist: $path"
done
[[ "$ACCEL" == "kvm" || "$ACCEL" == "tcg" ]] ||
    die_usage "--accel must be 'kvm' or 'tcg'"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
BOOT_SCRIPT="$REPO_ROOT/tools/x86/debian/boot_debian.sh"
CHECKLIST="$REPO_ROOT/tools/x86/debian/daily_use_checklist.sh"
for tool in debugfs e2fsck mke2fs python3 timeout "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done

mkdir -p -- "$WORKSPACE"
IMAGE="$WORKSPACE/daily.ext2"
USB_IMAGE="$WORKSPACE/usbstick.img"
WRAPPER="$WORKSPACE/qemu-daily.sh"
CLIENT="$WORKSPACE/daily_client.py"
CONSOLE_LOG="$WORKSPACE/console.txt"
SCREENSHOT="$WORKSPACE/screen.ppm"
SERIAL_SOCK="$WORKSPACE/serial.sock"
MONITOR_SOCK="$WORKSPACE/monitor.sock"
PIDFILE="$WORKSPACE/qemu.pid"
MATRIX="$WORKSPACE/matrix.tsv"

# The X server configuration the checklist starts with (same options as the
# Xorg gate: no udev, no automatic input devices).
XORG_CONF="$WORKSPACE/xorg-noudev.conf"
cat >"$XORG_CONF" <<'CONF'
Section "ServerFlags"
    Option "AutoAddDevices" "false"
    Option "AutoEnableDevices" "false"
EndSection
CONF

# An ext2 stick image with one file: the guest must mount and read it.
USB_ROOT="$WORKSPACE/usbstick-root"
rm -rf -- "$USB_ROOT"
mkdir -p -- "$USB_ROOT"
printf 'ASTERINAS-USB-OK\n' >"$USB_ROOT/hello.txt"
truncate -s 16M "$USB_IMAGE"
# The kernel ext2 driver needs 4 KiB blocks and has no ext_attr, dir_index,
# or resize inode.
mke2fs -q -F -t ext2 -b 4096 -O ^ext_attr,^resize_inode,^dir_index -d "$USB_ROOT" "$USB_IMAGE"

cat >"$WRAPPER" <<WRAPPER
#!/usr/bin/env bash
args=()
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        -chardev)
            case "\$2" in
                stdio,id=mux*) shift 2; continue ;;
            esac
            args+=("\$1" "\$2"); shift 2 ;;
        -device)
            case "\$2" in
                virtio-serial-pci|virtconsole,chardev=mux) shift 2; continue ;;
            esac
            args+=("\$1" "\$2"); shift 2 ;;
        -serial | -monitor) shift 2 ;;
        -append)
            args+=("\$1" "\${2/console=hvc0/console=ttyS0 earlycon}"); shift 2 ;;
        *)
            args+=("\$1"); shift ;;
    esac
done
printf '%s\n' "\$\$" >"$PIDFILE"
exec "$QEMU" "\${args[@]}" -vga none -device virtio-gpu-pci \\
    -device intel-hda -device hda-output,audiodev=audio0 \\
    -audiodev none,id=audio0 \\
    -device qemu-xhci -drive "if=none,id=usbstick,format=raw,file=$USB_IMAGE" \\
    -serial "unix:$SERIAL_SOCK,server=on,wait=off" \\
    -monitor "unix:$MONITOR_SOCK,server=on,wait=off"
WRAPPER
chmod +x "$WRAPPER"

cat >"$CLIENT" <<'PY'
import os
import socket
import sys
import time

console_path, serial_path, monitor_path, pid_path, shot_path, timeout = sys.argv[1:7]
timeout = float(timeout)
deadline = time.time() + timeout

# The redirection runs inside `sudo sh -c`: /dev/console is not writable by
# the login user.
COMMAND = (
    "sudo sh -c '/usr/local/bin/daily-use-checklist > /dev/console 2>&1'; "
    "echo DAILY_SHELL_END"
)

monitor = None


def connect(path, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.connect(path)
            return sock
        except OSError:
            time.sleep(0.05)
    return None


serial = connect(serial_path, 60)
if serial is None:
    print('DAILY_CLIENT no-serial', flush=True)
    sys.exit(2)
serial.settimeout(0.5)

buffer = b''
sent_user = sent_pass = sent_command = done = False
answered_usb = answered_suspend = shot = False
with open(console_path, 'wb') as out:
    while time.time() < deadline:
        try:
            chunk = serial.recv(4096)
        except socket.timeout:
            chunk = b''
        if chunk:
            buffer += chunk
            out.write(chunk)
            out.flush()

        if not sent_user and b'login:' in buffer:
            serial.sendall(b'asterinas\n')
            sent_user = True
        if sent_user and not sent_pass and b'Password:' in buffer:
            serial.sendall(b'asterinas\n')
            sent_pass = True
        if sent_pass and not sent_command and b'asterinas@' in buffer:
            time.sleep(0.2)
            serial.sendall((COMMAND + '\n').encode())
            sent_command = True

        if sent_command and monitor is None:
            monitor = connect(monitor_path, 5)
        if monitor is not None:
            if not answered_usb and b'DAILY_USB_WAIT' in buffer:
                monitor.sendall(b'device_add usb-storage,drive=usbstick\n')
                answered_usb = True
            if not answered_suspend and b'DAILY_SUSPEND_WAIT' in buffer:
                time.sleep(3)
                monitor.sendall(b'system_wakeup\n')
                answered_suspend = True
            if not shot and b'DAILY_DESKTOP=' in buffer:
                time.sleep(1)
                monitor.sendall(('screendump %s\n' % shot_path).encode())
                shot = True

        # The shell echoes the typed command, so the completion marker must be
        # the checklist's own last line.
        if sent_command and b'DAILY_DONE=ok' in buffer and not done:
            if monitor is not None:
                monitor.sendall(b'system_powerdown\n')
            done = True

        if done:
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
            except (OSError, ValueError):
                break

print('DAILY_CLIENT user=%s pass=%s command=%s usb=%s suspend=%s done=%s'
      % (sent_user, sent_pass, sent_command, answered_usb, answered_suspend,
         done), flush=True)
ok = sent_command and done and b'DAILY_DONE=ok' in buffer
sys.exit(0 if ok else 1)
PY

rm -f -- "$IMAGE" "$SERIAL_SOCK" "$MONITOR_SOCK" "$PIDFILE" "$CONSOLE_LOG" \
    "$SCREENSHOT" "$MATRIX"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    # Retire units an earlier gate left enabled in the base image.
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/xorgprobe.service\n'
    printf 'rm /etc/systemd/system/xorgprobe.service\n'
    printf 'write %s /usr/local/bin/daily-use-checklist\n' "$CHECKLIST"
    printf 'sif /usr/local/bin/daily-use-checklist mode 0100755\n'
    printf 'write %s /etc/X11/xorg-noudev.conf\n' "$XORG_CONF"
} | debugfs -w -f - "$IMAGE" >"$WORKSPACE/inject.log" 2>&1
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' "$WORKSPACE"
    exit 1
fi

timeout "$((TIMEOUT + 30))" python3 "$CLIENT" "$CONSOLE_LOG" "$SERIAL_SOCK" \
    "$MONITOR_SOCK" "$PIDFILE" "$SCREENSHOT" "$TIMEOUT" &
CLIENT_PID=$!

set +e
timeout "$((TIMEOUT + 60))" "$BOOT_SCRIPT" \
    --kernel "$KERNEL" \
    --initramfs "$INITRAMFS" \
    --root-image "$IMAGE" \
    --output "$WORKSPACE/boot.txt" \
    --loglevel 4 \
    --accel "$ACCEL" \
    --qemu "$WRAPPER" < /dev/null >"$WORKSPACE/boot.log" 2>&1 &
BOOT_PID=$!

wait "$CLIENT_PID"
CLIENT_STATUS=$?
kill "$BOOT_PID" 2>/dev/null
wait "$BOOT_PID" 2>/dev/null
set -e

if ! grep -aq "DAILY_DONE=ok" "$CONSOLE_LOG"; then
    printf 'FAIL (the checklist did not complete; see %s)\n' "$CONSOLE_LOG"
    exit 1
fi
if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (the client did not finish; see %s)\n' "$CONSOLE_LOG"
    exit 1
fi

# Build the matrix from the guest's markers.
python3 - "$CONSOLE_LOG" "$MATRIX" <<'PY'
import re
import sys

console, matrix = sys.argv[1], sys.argv[2]
text = open(console, "rb").read().decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

domains = [
    "DESKTOP", "APP", "APT", "CLIPBOARD", "INPUT_METHOD", "AUDIO", "USB",
    "SUSPEND",
]
found = {}
for name, value, *detail in re.findall(
    r"DAILY_([A-Z_]+)=(ok|fail|missing)\s*([^\r\n]*)", text
):
    found[name] = (value, " ".join(detail).strip())

missing = [name for name in domains if name not in found]
if missing:
    print("FAIL (no result for: %s)" % ", ".join(missing))
    raise SystemExit(1)

with open(matrix, "w") as out:
    out.write("domain\tresult\tdetail\n")
    width = max(len(name) for name in domains)
    print("%-*s  %-8s %s" % (width, "DOMAIN", "RESULT", "DETAIL"))
    print("%s  %s %s" % ("-" * width, "-" * 8, "-" * 30))
    for name in domains:
        value, detail = found[name]
        out.write("%s\t%s\t%s\n" % (name, value, detail))
        print("%-*s  %-8s %s" % (width, name, value, detail))
PY

printf 'DAILY (matrix written to %s)\n' "$MATRIX"
