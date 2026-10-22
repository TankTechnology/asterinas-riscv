#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Xorg graphics gate for the x86_64 Debian image.
#
# The gate logs in on the serial console (`ttyS0`) as the provisioned account,
# installs the X server over the QEMU slirp network, starts the modesetting
# driver on `/dev/dri/card0` (virtio-gpu), paints the root window with
# `xsetroot`, and requires:
#
#   * `xsetroot` to exit successfully,
#   * a monitor `screendump` of the virtio-gpu scanout to be red,
#   * the guest to power off when the monitor sends `system_powerdown`,
#   * `e2fsck -fn` to accept the image afterwards.
#
# With `--require-dri3` it additionally injects a static DRI3 probe into the
# image, requires `xdpyinfo` to advertise DRI3 and PRESENT, and requires the
# probe to complete one buffer exchange with the server.
#
# QEMU is started with `-vga none -device virtio-gpu-pci`; without `-vga none`
# the default VGA device owns the display and the virtio-gpu scanout is never
# the surface that `screendump` captures.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: xorg_check.sh --kernel ELF --initramfs CPIO --base-root IMG
                     --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy for the run (required).
  --workspace DIR     Image, scripts, screenshot and transcripts.
  --timeout SECONDS   Deadline for the boot, install and paint (default: 900).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).
  --require-dri3      Require DRI3/PRESENT and a buffer exchange to pass.
  --cc PATH           Host C compiler for the static probe (default: cc).

The guest account is `asterinas` (uid 1000) with password `asterinas` and
passwordless sudo. `apt-get` uses the QEMU slirp resolver (10.0.2.3).

Exit status is 0 only if xsetroot succeeds, the captured scanout is red,
the power button powers the guest off and `e2fsck -fn` accepts the image.
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
LOGLEVEL=4
ACCEL="kvm"
QEMU="qemu-system-x86_64"
REQUIRE_DRI3=0
CC="cc"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --initramfs | --base-root | --workspace | --timeout | \
            --loglevel | --accel | --qemu | --cc)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --initramfs) INITRAMFS="$2" ;;
                --base-root) BASE_ROOT="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --accel) ACCEL="$2" ;;
                --qemu) QEMU="$2" ;;
                --cc) CC="$2" ;;
            esac
            shift 2
            ;;
        --require-dri3) REQUIRE_DRI3=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ -n "$KERNEL" && -n "$INITRAMFS" && -n "$BASE_ROOT" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --initramfs, --base-root and --workspace are required"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$ACCEL" == "tcg" || "$ACCEL" == "kvm" ]] ||
    die_usage "--accel must be 'tcg' or 'kvm'"
TOOLS=(debugfs e2fsck python3 timeout "$QEMU")
[[ $REQUIRE_DRI3 -eq 1 ]] && TOOLS+=("$CC")
for tool in "${TOOLS[@]}"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done
for path in "$KERNEL" "$INITRAMFS" "$BASE_ROOT" "$WORKSPACE"; do
    [[ "$path" != *$'\n'* && "$path" != *$'\r'* && "$path" != *' '* ]] ||
        die_usage "path must not contain whitespace or newlines: $path"
done
[[ -f "$KERNEL" && -f "$INITRAMFS" && -f "$BASE_ROOT" ]] ||
    die_usage "an input path does not name a file"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
BOOT_SCRIPT="$REPO_ROOT/tools/x86/debian/boot_debian.sh"
[[ -x "$BOOT_SCRIPT" ]] || {
    printf 'error: boot script is missing: %s\n' "$BOOT_SCRIPT" >&2
    exit 1
}

mkdir -p -- "$WORKSPACE"
IMAGE="$WORKSPACE/xorg.ext2"
WRAPPER="$WORKSPACE/qemu-xorg.sh"
CLIENT="$WORKSPACE/xorg_client.py"
CONSOLE_LOG="$WORKSPACE/console.txt"
SCREENSHOT="$WORKSPACE/screen.ppm"
SERIAL_SOCK="$WORKSPACE/serial.sock"
MONITOR_SOCK="$WORKSPACE/monitor.sock"
PIDFILE="$WORKSPACE/qemu.pid"

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

COMMAND = (
    'echo XORG_BEGIN; '
    "sudo sh -c 'printf \"nameserver 10.0.2.3\\n\" > /etc/resolv.conf'; "
    'sudo apt-get update >/tmp/apt-update.log 2>&1; '
    'echo XORG_APT_UPDATE=$?; '
    'sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y '
    'xserver-xorg-core x11-xserver-utils x11-utils xterm '
    '>/tmp/apt-install.log 2>&1; '
    'echo XORG_APT_INSTALL=$?; '
    "sudo sh -c 'printf \"Section \\\"ServerFlags\\\"\\n"
    "    Option \\\"AutoAddDevices\\\" \\\"false\\\"\\n"
    "    Option \\\"AutoEnableDevices\\\" \\\"false\\\"\\n"
    "EndSection\\n\\\" > /etc/X11/xorg-noudev.conf'; "
    # virtio-gpu has no 3D engine in QEMU, so force the software KMS driver
    # (the same choice the RISC-V gate makes) and keep the server log on the
    # console, where it survives the power-off. The redirection runs inside
    # `sudo sh -c`: /dev/console is not writable by the login user.
    'sudo sh -c "env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe EGL_LOG_LEVEL=debug '
    'LIBGL_DEBUG=verbose /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp '
    '-config /etc/X11/xorg-noudev.conf -logfile /dev/console '
    '>>/dev/console 2>&1" & '
    'i=0; while [ ! -S /tmp/.X11-unix/X0 ] && [ $i -lt 40 ]; do '
    'sleep 1; i=$((i+1)); done; '
    'echo XORG_SOCKET=$i; '
    'DISPLAY=:0 xterm -geometry 60x20+40+40 2>/tmp/xterm.err & '
    'sleep 3; pgrep -f xterm >/dev/null; echo XORG_XTERM=$?; '
    'sed "s/^/XTERM /" /tmp/xterm.err; '
    'pkill -f xterm; sleep 1; '
    'sudo env DISPLAY=:0 xdpyinfo -queryExtensions 2>/dev/null '
    '| grep -E "DRI[0-9]|PRESENT" | sed "s/^/XDINFO /"; '
    'sudo env DISPLAY=:0 xdpyinfo -queryExtensions 2>/dev/null '
    '| grep -q DRI3; echo XORG_XDPYINFO_DRI3=$?; '
    'sudo env DISPLAY=:0 xdpyinfo -queryExtensions 2>/dev/null '
    '| grep -q Present; echo XORG_XDPYINFO_PRESENT=$?; '
    'if [ -x /usr/local/bin/dri3-probe ]; then '
    'sudo env DISPLAY=:0 /usr/local/bin/dri3-probe 0; echo XORG_DRI3=$?; fi; '
    'DISPLAY=:0 xsetroot -solid red; echo XORG_SETROOT=$?; '
    "printf 'XORG_%s_END\\n' ok"
)


def connect(path, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.connect(path)
            return sock
        except OSError:
            time.sleep(0.05)
    return None


serial = connect(serial_path, 30)
if serial is None:
    print('XORG_CLIENT no-serial', flush=True)
    sys.exit(2)
serial.settimeout(0.5)

buffer = b''
sent_user = sent_pass = sent_command = done = False
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
        if sent_command and b'XORG_ok_END' in buffer and not done:
            time.sleep(1)
            monitor = connect(monitor_path, 10)
            if monitor is None:
                print('XORG_CLIENT no-monitor', flush=True)
                break
            monitor.sendall(('screendump %s\n' % shot_path).encode())
            time.sleep(1)
            monitor.sendall(b'system_powerdown\n')
            monitor.close()
            done = True
        if done:
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
            except (OSError, ValueError):
                break
print('XORG_CLIENT user=%s pass=%s command=%s done=%s'
      % (sent_user, sent_pass, sent_command, done), flush=True)
ok = sent_user and sent_pass and sent_command and done and b'XORG_ok_END' in buffer
sys.exit(0 if ok else 1)
PY

rm -f -- "$IMAGE" "$SERIAL_SOCK" "$MONITOR_SOCK" "$PIDFILE" "$CONSOLE_LOG" \
    "$SCREENSHOT"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    # Retire the oneshot units an earlier gate installed: `asterinas-debian-ext4`
    # would boot the image on its own, and `xorgprobe` starts an X server on :0
    # before this gate does, leaving a socket and a lock behind.
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/xorgprobe.service\n'
    printf 'rm /etc/systemd/system/xorgprobe.service\n'
} | debugfs -w -f - "$IMAGE" >/dev/null 2>&1
if [[ $REQUIRE_DRI3 -eq 1 ]]; then
    DRI3_PROBE="$WORKSPACE/dri3-probe"
    "$CC" -static -O2 -Wall -Wextra -o "$DRI3_PROBE" \
        "$REPO_ROOT/tools/drm/dri3_probe.c"
    {
        printf 'write %s /usr/local/bin/dri3-probe\n' "$DRI3_PROBE"
        printf 'sif /usr/local/bin/dri3-probe mode 0100755\n'
    } | debugfs -w -f - "$IMAGE" >/dev/null 2>&1
fi
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' \
        "$WORKSPACE"
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
    --loglevel "$LOGLEVEL" \
    --accel "$ACCEL" \
    --qemu "$WRAPPER" < /dev/null >"$WORKSPACE/boot.log" 2>&1 &
BOOT_PID=$!

wait "$CLIENT_PID"
CLIENT_STATUS=$?
kill "$BOOT_PID" 2>/dev/null
wait "$BOOT_PID" 2>/dev/null
set -e

if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (the Xorg flow did not complete; see %s and %s)\n' \
        "$CONSOLE_LOG" "$WORKSPACE/boot.log"
    exit 1
fi

MARKERS=('XORG_APT_UPDATE=0' 'XORG_APT_INSTALL=0' 'XORG_XTERM=0' \
    'XORG_SETROOT=0' 'XORG_ok_END')
if [[ $REQUIRE_DRI3 -eq 1 ]]; then
    MARKERS+=('XORG_XDPYINFO_DRI3=0' 'XORG_XDPYINFO_PRESENT=0' 'XORG_DRI3=0')
fi
for marker in "${MARKERS[@]}"; do
    if ! grep -aqF -- "$marker" "$CONSOLE_LOG"; then
        printf 'FAIL (console is missing "%s"); see %s\n' "$marker" "$CONSOLE_LOG"
        exit 1
    fi
done

if [[ ! -s "$SCREENSHOT" ]]; then
    printf 'FAIL (the monitor produced no screendump; see %s)\n' "$WORKSPACE"
    exit 1
fi

if ! python3 - "$SCREENSHOT" <<'PY'
import sys

path = sys.argv[1]
with open(path, 'rb') as f:
    data = f.read()
parts = data.split(b'\n', 3)
if parts[0] != b'P6' or len(parts) < 4:
    print('FAIL (screendump is not a P6 PPM)')
    sys.exit(1)
width, height = map(int, parts[1].split())
pixels = parts[3]
total = width * height
red = 0
sampled = 0
for i in range(0, total, 41):
    r = pixels[3 * i]
    g = pixels[3 * i + 1]
    b = pixels[3 * i + 2]
    sampled += 1
    if r > 150 and g < 100 and b < 100:
        red += 1
ratio = red / sampled
print('screendump %dx%d red=%.3f' % (width, height, ratio))
sys.exit(0 if ratio >= 0.9 else 1)
PY
then
    printf 'FAIL (the captured scanout is not the painted root window; see %s)\n' \
        "$SCREENSHOT"
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s/fsck-after.txt)\n' "$WORKSPACE"
    exit 1
fi
rm -f -- "$IMAGE"

printf 'PASS (serial login, Xorg modeset on virtio-gpu, red root window, power-button power-off)\n'
