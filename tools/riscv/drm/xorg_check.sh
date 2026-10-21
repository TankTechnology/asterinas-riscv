#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# RISC-V Xorg gate on the shared DRM core.
#
# Takes a Debian `desktop-drm` root image (built by
# `tools/riscv/debian/rootfs/build_rootfs.sh --profile desktop-drm`), injects a
# oneshot unit that starts Xorg on /dev/dri/card0 and paints the root window
# red through a raw X11 client, boots it through the Stage1 initramfs with a
# virtio-gpu MMIO device, and requires:
#
#   * `XORG_GATE_PAINT=0` on the serial console,
#   * a monitor `screendump` whose sampled pixels are at least 90% red.
#
# The injection avoids the image's own graphical session, which waits on
# udev/logind and a virtual terminal; this gate only needs the X server and the
# DRM node, exactly like the x86 gate.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: xorg_check.sh --kernel IMAGE --stage1 CPIO --base-image EXT2
                     --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas RISC-V kernel Image (Sv39, required).
  --stage1 PATH       Stage1 initramfs cpio (required).
  --base-image PATH   Pristine Debian desktop-drm ext2 image (required).
  --workspace DIR     Working copy, transcripts and screendump.
  --timeout SECS      Deadline for boot, Xorg and paint (default: 480).
  --loglevel N        Kernel log level (default: 4, Linux numbering).
  --cc PATH           RISC-V cross compiler (default: riscv64-linux-gnu-gcc).
  --qemu PATH         qemu-system-riscv64 (default: from PATH).
  --vnc-display N     VNC display number; a free one is picked by default.

Exit status is 0 only if Xorg comes up, the paint client succeeds, and the
captured scanout is red.
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
TIMEOUT=480
LOGLEVEL=4
CC="riscv64-linux-gnu-gcc"
QEMU="qemu-system-riscv64"
VNC_DISPLAY=""

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
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ -n "$KERNEL" && -n "$STAGE1" && -n "$BASE_IMAGE" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel, --stage1, --base-image and --workspace are required"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$LOGLEVEL" =~ ^[0-9]+$ ]] || die_usage "--loglevel must be a non-negative integer"
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
PAINT="$WORKSPACE/x11-paint"
SERIAL="$WORKSPACE/console.txt"
MONITOR="$WORKSPACE/monitor.sock"
SCREENSHOT="$WORKSPACE/screen.ppm"
QEMU_LOG="$WORKSPACE/qemu.log"

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

"$CC" -static -O2 -Wall -Wextra -o "$PAINT" "$REPO_ROOT/tools/drm/x11_paint.c"

rm -f -- "$IMAGE" "$SERIAL" "$MONITOR" "$SCREENSHOT"
cp --sparse=always -- "$BASE_IMAGE" "$IMAGE"

cat >"$WORKSPACE/inject.debugfs" <<EOF
mkdir /usr/local
mkdir /usr/local/bin
write $PAINT /usr/local/bin/x11-paint
sif /usr/local/bin/x11-paint mode 0100755
write $SCRIPT_DIR/xorg_gate.sh /usr/local/bin/xorg-gate.sh
sif /usr/local/bin/xorg-gate.sh mode 0100755
write $SCRIPT_DIR/xorg-gate.service /etc/systemd/system/xorg-gate.service
write $SCRIPT_DIR/xorg-noudev.conf /etc/X11/xorg-noudev.conf
symlink /etc/systemd/system/multi-user.target.wants/xorg-gate.service /etc/systemd/system/xorg-gate.service
rm /etc/systemd/system/default.target
symlink /etc/systemd/system/default.target /lib/systemd/system/multi-user.target
EOF
debugfs -w -f "$WORKSPACE/inject.debugfs" "$IMAGE" >"$WORKSPACE/inject.log" 2>&1
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (injected image is not clean; see %s/fsck-prepared.txt)\n' \
        "$WORKSPACE"
    exit 1
fi

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
    -vnc "127.0.0.1:$VNC_DISPLAY" \
    -serial "file:$SERIAL" \
    -monitor "unix:$MONITOR,server=on,wait=off" \
    >"$QEMU_LOG" 2>&1 &
QEMU_PID=$!

cleanup() {
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
}
trap cleanup EXIT

STATUS="timeout"
for _ in $(seq 1 "$TIMEOUT"); do
    if grep -aq 'XORG_GATE_PAINT=0' "$SERIAL" 2>/dev/null; then
        STATUS="pass"
        break
    fi
    if grep -aq 'XORG_GATE_SERVER_FAILED\|XORG_GATE_PAINT=[1-9]\|Uncaught panic\|Kernel panic' \
        "$SERIAL" 2>/dev/null; then
        STATUS="fail"
        break
    fi
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        STATUS="qemu-exited"
        break
    fi
    sleep 1
done

if [[ "$STATUS" != "pass" ]]; then
    grep -a 'XORG_GATE_\|XPAINT_\|Xorg\|panic' "$SERIAL" 2>/dev/null |
        sed 's/\x1b\[[0-9;]*m//g' | tail -30 || true
    printf 'FAIL (%s; see %s and %s)\n' "$STATUS" "$SERIAL" "$QEMU_LOG"
    exit 1
fi

# Let the server present the cleared root window, then capture it.
sleep 2
python3 - "$MONITOR" "$SCREENSHOT" <<'PY'
import socket
import sys
import time

monitor, screenshot = sys.argv[1], sys.argv[2]
sock = socket.socket(socket.AF_UNIX)
sock.settimeout(10)
sock.connect(monitor)
sock.sendall(("screendump %s\n" % screenshot).encode())
time.sleep(1)
sock.sendall(b"quit\n")
time.sleep(1)
sock.close()
PY
cleanup
trap - EXIT

grep -a 'XORG_GATE_\|XPAINT_' "$SERIAL" | sed 's/\x1b\[[0-9;]*m//g'

if [[ ! -s "$SCREENSHOT" ]]; then
    printf 'FAIL (no screendump was written; see %s)\n' "$QEMU_LOG"
    exit 1
fi

if ! python3 - "$SCREENSHOT" <<'PY'
import sys

path = sys.argv[1]
with open(path, "rb") as f:
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
    printf 'FAIL (the captured scanout is not the painted root window; see %s)\n' \
        "$SCREENSHOT"
    exit 1
fi

printf 'PASS (RISC-V Xorg modesetting on virtio-gpu: red root window, screendump)\n'
