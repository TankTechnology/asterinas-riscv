#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# RISC-V graphics gate for the shared `aster-drm` core.
#
# Boots an Asterinas RISC-V kernel (Sv39) with a virtio-gpu MMIO device and a
# one-file initramfs whose `/init` is `tools/drm/virtio_gpu_probe.c`. The probe
# drives the same legacy KMS ioctl sequence Xorg's modesetting driver uses,
# paints four colour bars into a dumb buffer and makes it the scanout. The gate
# then captures the host-side scanout through the QEMU monitor and requires the
# bars to be visible.
#
# This is the RISC-V half of P1 in `docs/porting/drm-shared-core-design.md`:
# the device layer, the DRM plugin and the probe are the same code as the x86
# gate, only the transport (virtio-mmio instead of virtio-pci) differs.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: virtio_gpu_check.sh --kernel IMAGE --workspace DIR [OPTIONS]

Options:
  --kernel PATH     Asterinas RISC-V kernel Image (Sv39 build, required).
  --workspace DIR   Directory for the initramfs, transcript and screendump.
  --timeout SECS    Deadline for the boot and probe (default: 180).
  --loglevel N      Kernel log level (default: 4, Linux numbering).
  --vnc-display N   VNC display number; a free one is picked by default.
  --cc PATH         Cross compiler (default: riscv64-linux-gnu-gcc).
  --qemu PATH       qemu-system-riscv64 binary (default: from PATH).

Exit status is 0 only if the probe prints `DRMPROBE_PASS` and the captured
scanout is the four-bar image the probe drew.
EOF
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

KERNEL=""
WORKSPACE=""
TIMEOUT=180
LOGLEVEL=4
VNC_DISPLAY=""
CC=""
QEMU="qemu-system-riscv64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --workspace | --timeout | --loglevel | --vnc-display | \
            --cc | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
                --vnc-display) VNC_DISPLAY="$2" ;;
                --cc) CC="$2" ;;
                --qemu) QEMU="$2" ;;
            esac
            shift 2
            ;;
        -h | --help) usage; exit 0 ;;
        *) die_usage "unknown argument: $1" ;;
    esac
done

[[ -n "$KERNEL" && -n "$WORKSPACE" ]] ||
    die_usage "--kernel and --workspace are required"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die_usage "--timeout must be a positive integer"
[[ "$LOGLEVEL" =~ ^[0-9]+$ ]] || die_usage "--loglevel must be a non-negative integer"
[[ -f "$KERNEL" ]] || die_usage "the kernel image does not exist: $KERNEL"
for tool in python3 timeout cpio "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
BUILD_PROBE="$REPO_ROOT/tools/drm/build_probe.sh"
[[ -x "$BUILD_PROBE" ]] || {
    printf 'error: probe builder is missing: %s\n' "$BUILD_PROBE" >&2
    exit 1
}

mkdir -p -- "$WORKSPACE"
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

BUILD_ARGS=(--arch riscv64 --output "$WORKSPACE/probe")
[[ -n "$CC" ]] && BUILD_ARGS+=(--cc "$CC")
INITRAMFS="$("$BUILD_PROBE" "${BUILD_ARGS[@]}")"

rm -f -- "$SERIAL" "$MONITOR" "$SCREENSHOT"
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
for _ in $(seq 1 $((TIMEOUT / 2))); do
    if grep -aq 'DRMPROBE_PASS' "$SERIAL" 2>/dev/null; then
        STATUS="pass"
        break
    fi
    if grep -aq 'DRMPROBE_FAIL' "$SERIAL" 2>/dev/null; then
        STATUS="fail"
        break
    fi
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        STATUS="qemu-exited"
        break
    fi
    sleep 2
done

if [[ "$STATUS" != "pass" ]]; then
    grep -a 'DRMPROBE\|panic\|virtio-gpu' "$SERIAL" 2>/dev/null | tail -20 || true
    printf 'FAIL (%s; see %s and %s)\n' "$STATUS" "$SERIAL" "$QEMU_LOG"
    exit 1
fi

# Give the device a moment to present the flush before capturing it.
sleep 1
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

grep -a 'DRMPROBE_' "$SERIAL" | sed 's/\x1b\[[0-9;]*m//g'

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
expected = {
    (255, 0, 0): "red",
    (0, 255, 0): "green",
    (0, 0, 255): "blue",
    (255, 255, 255): "white",
}
seen = {name: 0 for name in expected.values()}
sampled = 0
black = 0
for y in range(0, height, 37):
    row = y * width * 3
    for x in range(0, width, 29):
        i = row + x * 3
        colour = (pixels[i], pixels[i + 1], pixels[i + 2])
        sampled += 1
        if colour in expected:
            seen[expected[colour]] += 1
        if sum(colour) < 60:
            black += 1
print("screendump %dx%d sampled=%d bars=%s black=%d"
      % (width, height, sampled,
         "/".join("%s:%d" % (name, count) for name, count in seen.items()),
         black))
ok = width == 1024 and height == 768 and black == 0 and all(seen.values())
raise SystemExit(0 if ok else 1)
PY
then
    printf 'FAIL (the captured scanout does not show the probe bars; see %s)\n' \
        "$SCREENSHOT"
    exit 1
fi

printf 'PASS (virtio-gpu 2D on RISC-V: KMS probe, four-bar scanout, screendump)\n'
