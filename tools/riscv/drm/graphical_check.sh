#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# RISC-V graphical daily-use gate.
#
# Boots the Debian `desktop-drm` image through Stage1, logs in on the serial
# console as `root` (the image ships no `sudo`, so the session runs as the
# account agetty logs in automatically), starts Xorg on /dev/dri/card0,
# launches `xterm`, runs the DRI3
# buffer-exchange probe, paints the root window red, captures the scanout
# through the QEMU monitor, powers the guest off from inside, and finally runs
# `e2fsck -fn`.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: graphical_check.sh --kernel IMAGE --stage1 CPIO --base-image EXT2
                          --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas RISC-V kernel Image (Sv39, required).
  --stage1 PATH       Stage1 initramfs cpio (required).
  --base-image PATH   Pristine Debian desktop-drm ext2 image (required).
  --workspace DIR     Working copy, transcripts and screendump.
  --timeout SECS      Deadline for boot, login and the session (default: 900).
  --loglevel N        Kernel log level (default: 4).
  --cc PATH           RISC-V cross compiler (default: riscv64-linux-gnu-gcc).
  --qemu PATH         qemu-system-riscv64.
  --vnc-display N     VNC display number; a free one is picked by default.
  --require-dri3      Require the DRI3 probe to pass as well.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

KERNEL=""
STAGE1=""
BASE_IMAGE=""
WORKSPACE=""
TIMEOUT=900
LOGLEVEL=4
CC="riscv64-linux-gnu-gcc"
QEMU="qemu-system-riscv64"
VNC_DISPLAY=""
REQUIRE_DRI3=0

while (( $# > 0 )); do
    case "$1" in
        --kernel | --stage1 | --base-image | --workspace | --timeout | \
            --loglevel | --cc | --qemu | --vnc-display)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --stage1) STAGE1="$2" ;;
                --base-image) BASE_IMAGE="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --cc) CC="$2" ;;
                --qemu) QEMU="$2" ;;
                --vnc-display) VNC_DISPLAY="$2" ;;
            esac
            shift 2
            ;;
        --require-dri3) REQUIRE_DRI3=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ -n "$KERNEL" && -n "$STAGE1" && -n "$BASE_IMAGE" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --stage1, --base-image and --workspace are required"
for path in "$KERNEL" "$STAGE1" "$BASE_IMAGE"; do
    [[ -f "$path" ]] || die_usage "input does not exist: $path"
done
for tool in debugfs e2fsck python3 timeout "$QEMU" "$CC"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"

mkdir -p -- "$WORKSPACE"
IMAGE="$WORKSPACE/root.ext2"
SERIAL_SOCK="$WORKSPACE/serial.sock"
MONITOR="$WORKSPACE/monitor.sock"
SCREENSHOT="$WORKSPACE/screen.ppm"
CONSOLE="$WORKSPACE/console.txt"
QEMU_LOG="$WORKSPACE/qemu.log"
DRI3_PROBE="$WORKSPACE/dri3-probe"
PAINT="$WORKSPACE/x11-paint"

if [[ -z "$VNC_DISPLAY" ]]; then
    VNC_DISPLAY="$(python3 - <<'PY'
import socket
for display in range(41, 160):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 5900 + display))
    except OSError:
        continue
    finally:
        sock.close()
    print(display)
    break
else:
    raise SystemExit("no free VNC display")
PY
)"
fi

"$CC" -static -O2 -Wall -Wextra -o "$DRI3_PROBE" "$REPO_ROOT/tools/drm/dri3_probe.c"
"$CC" -static -O2 -Wall -Wextra -o "$PAINT" "$REPO_ROOT/tools/drm/x11_paint.c"

rm -f -- "$IMAGE" "$SERIAL_SOCK" "$MONITOR" "$SCREENSHOT" "$CONSOLE"
cp --sparse=always -- "$BASE_IMAGE" "$IMAGE"

# --- Image preparation: the serial login service and the probes ---

# `serial-getty@.service` binds to `dev-ttyS0.device`, a unit systemd
# synthesizes from a udev "add" event. Asterinas does not emit udev events
# yet, so the device unit never appears, the getty is stopped after its 90 s
# timeout, and the boot waits for it. The generated instance cannot be
# reconfigured away, so mask it and provide a login service of our own that
# needs nothing but the device node.
cat >"$WORKSPACE/asterinas-serial-login.service" <<'EOF'
[Unit]
Description=Asterinas serial login on ttyS0
After=systemd-user-sessions.service

[Service]
Type=idle
KeyringMode=inherit
ExecStart=-/sbin/agetty --noclear --autologin root ttyS0 115200
Restart=always

[Install]
WantedBy=multi-user.target
EOF

cat >"$WORKSPACE/inject.debugfs" <<EOF
write $DRI3_PROBE /usr/local/bin/dri3-probe
sif /usr/local/bin/dri3-probe mode 0100755
write $PAINT /usr/local/bin/x11-paint
sif /usr/local/bin/x11-paint mode 0100755
write $SCRIPT_DIR/xorg-noudev.conf /etc/X11/xorg-noudev.conf
rm /etc/systemd/system/multi-user.target.wants/asterinas-desktop-drm.service
rm /etc/systemd/system/multi-user.target.wants/asterinas-desktop-drm-evidence.service
rm /etc/systemd/system/asterinas-desktop-drm.service
rm /etc/systemd/system/asterinas-desktop-drm-evidence.service
symlink /etc/systemd/system/serial-getty@ttyS0.service /dev/null
write $WORKSPACE/asterinas-serial-login.service /etc/systemd/system/asterinas-serial-login.service
symlink /etc/systemd/system/multi-user.target.wants/asterinas-serial-login.service /etc/systemd/system/asterinas-serial-login.service
EOF
debugfs -w -f "$WORKSPACE/inject.debugfs" "$IMAGE" >"$WORKSPACE/inject.log" 2>&1
e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1 || {
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' "$WORKSPACE"
    exit 1
}

"$QEMU" \
    -M virt \
    -cpu rv64,svpbmt=true,zkr=true,sv48=false,svadu=false,svade=true \
    -m 2G \
    -smp 1 \
    -no-reboot \
    -kernel "$KERNEL" \
    -initrd "$STAGE1" \
    -append "console=ttyS0 loglevel=$LOGLEVEL init=/init -- --root-init=systemd --root-fs=ext4" \
    -drive "if=none,format=raw,file=$IMAGE,id=rootfs" \
    -device virtio-blk-device,drive=rootfs \
    -device virtio-gpu-device \
    -device virtio-keyboard-device \
    -serial "unix:$SERIAL_SOCK,server=on,wait=off" \
    -monitor "unix:$MONITOR,server=on,wait=off" \
    -vnc "127.0.0.1:$VNC_DISPLAY" \
    >"$QEMU_LOG" 2>&1 &
QEMU_PID=$!

cleanup() {
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
}
trap cleanup EXIT

python3 - "$CONSOLE" "$SERIAL_SOCK" "$MONITOR" "$SCREENSHOT" "$TIMEOUT" <<'PY'
import os
import socket
import sys
import time

console_path, serial_path, monitor_path, shot_path, deadline_seconds = sys.argv[1:6]
deadline = time.time() + float(deadline_seconds)

COMMAND = (
    'export DISPLAY=:0; '
    # The login session is root, so the server can log straight to the
    # console where the transcript survives the power-off.
    'env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe EGL_LOG_LEVEL=debug '
    'LIBGL_DEBUG=verbose /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp '
    '-config /etc/X11/xorg-noudev.conf -logfile /dev/console '
    '>>/dev/console 2>&1 & '
    'i=0; while [ ! -S /tmp/.X11-unix/X0 ] && [ $i -lt 60 ]; do sleep 1; i=$((i+1)); done; '
    'echo XORG_SOCKET=$i; '
    'xterm -geometry 60x20+40+40 2>/tmp/xterm.err & sleep 3; '
    'sed "s/^/XTERM /" /tmp/xterm.err; '
    'pgrep xterm >/dev/null; echo XORG_XTERM=$?; '
    'pkill xterm; sleep 1; '
    'env DISPLAY=:0 xdpyinfo -queryExtensions 2>/dev/null '
    '| grep -E "DRI[0-9]|Present" | sed "s/^/XDINFO /"; '
    'env DISPLAY=:0 xdpyinfo -queryExtensions 2>/dev/null '
    '| grep -q DRI3; echo XORG_XDPYINFO_DRI3=$?; '
    'env DISPLAY=:0 xdpyinfo -queryExtensions 2>/dev/null '
    '| grep -q Present; echo XORG_XDPYINFO_PRESENT=$?; '
    'if [ -x /usr/local/bin/dri3-probe ]; then '
    'env DISPLAY=:0 /usr/local/bin/dri3-probe 0; echo XORG_DRI3=$?; fi; '
    'if [ -x /usr/local/bin/x11-paint ]; then '
    'env DISPLAY=:0 /usr/local/bin/x11-paint 0; echo XORG_SETROOT=$?; fi; '
    "printf 'XORG_%s_END\\n' ok"
)


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
    print('GRAPHICAL_CLIENT no-serial', flush=True)
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
        # The login service runs `agetty --autologin asterinas`: the account
        # still comes from the image's own passwd/shadow, but no password is
        # prompted for. The password login path is exercised by the x86 gate.
        if not sent_user and b'login:' in buffer:
            sent_user = True
        if not sent_command and b'root@' in buffer:
            time.sleep(0.3)
            serial.sendall((COMMAND + '\n').encode())
            sent_command = True
        if sent_command and b'XORG_ok_END' in buffer and not done:
            time.sleep(2)
            monitor = connect(monitor_path, 10)
            if monitor is None:
                print('GRAPHICAL_CLIENT no-monitor', flush=True)
                break
            monitor.sendall(('screendump %s\n' % shot_path).encode())
            time.sleep(1)
            # Graceful shutdown from inside the guest, so the image stays
            # consistent for e2fsck.
            serial.sendall(b'systemctl poweroff\n')
            monitor.close()
            # QEMU removes the serial socket when it exits.
            end = time.time() + 120
            while time.time() < end and os.path.exists(serial_path):
                try:
                    chunk = serial.recv(4096)
                    if chunk:
                        buffer += chunk
                        out.write(chunk)
                        out.flush()
                except socket.timeout:
                    time.sleep(1)
                except OSError:
                    break
            done = True
        if done and not os.path.exists(serial_path):
            break
print('GRAPHICAL_CLIENT autologin=%s command=%s done=%s'
      % (sent_user, sent_command, done), flush=True)
ok = sent_command and done and b'XORG_ok_END' in buffer
sys.exit(0 if ok else 1)
PY
CLIENT_STATUS=$?

if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (the graphical session did not complete; see %s)\n' "$CONSOLE"
    exit 1
fi

# Give QEMU a moment to exit after the guest power-off; otherwise stop it.
for _ in $(seq 1 30); do
    kill -0 "$QEMU_PID" 2>/dev/null || break
    sleep 2
done
if kill -0 "$QEMU_PID" 2>/dev/null; then
    printf 'note: the guest did not power off; stopping QEMU\n'
fi
cleanup
trap - EXIT

MARKERS=('XORG_XTERM=0' 'XORG_SETROOT=0' 'XORG_ok_END')
if [[ $REQUIRE_DRI3 -eq 1 ]]; then
    MARKERS+=('XORG_XDPYINFO_DRI3=0' 'XORG_XDPYINFO_PRESENT=0' 'XORG_DRI3=0')
fi
for marker in "${MARKERS[@]}"; do
    if ! grep -aqF -- "$marker" "$CONSOLE"; then
        printf 'FAIL (console is missing "%s")\n' "$marker"
        exit 1
    fi
done
if [[ $REQUIRE_DRI3 -eq 1 ]] && ! grep -aqF 'XORG_DRI3=0' "$CONSOLE"; then
    printf 'FAIL (the DRI3 probe did not pass; see %s)\n' "$CONSOLE"
    exit 1
fi

if ! python3 - "$SCREENSHOT" <<'PY'
import sys

with open(sys.argv[1], "rb") as f:
    data = f.read()
parts = data.split(b"\n", 3)
if len(parts) < 4 or parts[0] != b"P6":
    print("FAIL (screendump is not a P6 PPM)")
    raise SystemExit(1)
width, height = map(int, parts[1].split())
pixels = parts[3]
sampled = red = 0
for y in range(0, height, 13):
    row = y * width * 3
    for x in range(0, width, 11):
        i = row + x * 3
        sampled += 1
        if pixels[i] > 150 and pixels[i + 1] < 100 and pixels[i + 2] < 100:
            red += 1
ratio = red / sampled
print("screendump %dx%d red=%.3f" % (width, height, ratio))
raise SystemExit(0 if ratio >= 0.9 else 1)
PY
then
    printf 'FAIL (the captured scanout is not the painted root window)\n'
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s/fsck-after.txt)\n' "$WORKSPACE"
    exit 1
fi

printf 'PASS (RISC-V graphical session: serial login, Xorg, xterm, red root window, power-off)\n'
