#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# `aplay` gate for the x86_64 Debian image.
#
# Boots the image with QEMU's `intel-hda` controller and an `hda-output`
# codec whose audio backend is a WAV file, installs `alsa-utils`, plays an
# injected 440 Hz tone with `aplay -D hw:0,0`, and checks both the tool's exit
# status and the samples the host actually captured.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: audio_gate.sh --kernel ELF --initramfs CPIO --base-root IMG
                     --workspace DIR [OPTIONS]

Options:
  --kernel PATH       Asterinas x86_64 multiboot2 ELF (required).
  --initramfs PATH    Stage1 cpio initramfs (required).
  --base-root PATH    Debian ext4 image to copy for the run (required).
  --workspace DIR     Image, tone, transcripts and the captured WAV.
  --timeout SECONDS   Deadline for login, install, playback (default: 900).
  --accel MODE        'kvm' or 'tcg' (default: kvm).
  --qemu PATH         QEMU binary or wrapper (default: qemu-system-x86_64).
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
for tool in debugfs e2fsck python3 timeout "$QEMU"; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'error: required tool not found: %s\n' "$tool" >&2
        exit 1
    }
done

mkdir -p -- "$WORKSPACE"
IMAGE="$WORKSPACE/audio.ext2"
WRAPPER="$WORKSPACE/qemu-audio.sh"
CLIENT="$WORKSPACE/audio_client.py"
CONSOLE_LOG="$WORKSPACE/console.txt"
CAPTURE="$WORKSPACE/capture.wav"
TONE="$WORKSPACE/tone.wav"
CHECK="$WORKSPACE/audio-check.sh"
SERIAL_SOCK="$WORKSPACE/serial.sock"
MONITOR_SOCK="$WORKSPACE/monitor.sock"
PIDFILE="$WORKSPACE/qemu.pid"

# Half a second of a 440 Hz square wave at 48 kHz/16-bit/stereo.
python3 - "$TONE" <<'PY'
import struct
import sys
import wave

frames = 24000
samples = []
for frame in range(frames):
    value = 8000 if (frame // 54) % 2 == 0 else -8000
    samples += [value, value]

with wave.open(sys.argv[1], "wb") as wav:
    wav.setnchannels(2)
    wav.setsampwidth(2)
    wav.setframerate(48000)
    wav.writeframes(struct.pack("<%dh" % len(samples), *samples))
PY

cat >"$CHECK" <<'CHECK'
#!/bin/sh
exec >/dev/console 2>&1
apt-get install -y alsa-utils
echo AUDIO_APT=$?
aplay -D hw:0,0 /root/tone.wav
echo AUDIO_APLAY=$?
echo AUDIO_CHECK_DONE
CHECK

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
exec "$QEMU" "\${args[@]}" \\
    -device intel-hda -device hda-output,audiodev=audio0 \\
    -audiodev "wav,id=audio0,path=$CAPTURE" \\
    -serial "unix:$SERIAL_SOCK,server=on,wait=off" \\
    -monitor "unix:$MONITOR_SOCK,server=on,wait=off"
WRAPPER
chmod +x "$WRAPPER"

cat >"$CLIENT" <<'PY'
import os
import socket
import sys
import time

console_path, serial_path, monitor_path, pid_path, timeout = sys.argv[1:6]
timeout = float(timeout)
deadline = time.time() + timeout


def connect(path, wait):
    end = time.time() + wait
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
    print('AUDIO_CLIENT no-serial', flush=True)
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
            serial.sendall(b'sudo /usr/local/bin/audio-check\n')
            sent_command = True
        if sent_command and b'AUDIO_CHECK_DONE' in buffer and not done:
            time.sleep(1)
            monitor = connect(monitor_path, 10)
            if monitor is not None:
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
print('AUDIO_CLIENT user=%s pass=%s command=%s done=%s'
      % (sent_user, sent_pass, sent_command, done), flush=True)
ok = sent_user and sent_pass and sent_command and done
sys.exit(0 if ok else 1)
PY

rm -f -- "$IMAGE" "$SERIAL_SOCK" "$MONITOR_SOCK" "$PIDFILE" "$CONSOLE_LOG" \
    "$CAPTURE"
cp --sparse=always -- "$BASE_ROOT" "$IMAGE"
{
    printf 'rm /etc/systemd/system/multi-user.target.wants/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/asterinas-debian-ext4.service\n'
    printf 'rm /etc/systemd/system/multi-user.target.wants/xorgprobe.service\n'
    printf 'rm /etc/systemd/system/xorgprobe.service\n'
    printf 'write %s /usr/local/bin/audio-check\n' "$CHECK"
    printf 'sif /usr/local/bin/audio-check mode 0100755\n'
    printf 'write %s /root/tone.wav\n' "$TONE"
} | debugfs -w -f - "$IMAGE" >"$WORKSPACE/inject.log" 2>&1
if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-prepared.txt" 2>&1; then
    printf 'FAIL (prepared image is not clean; see %s/fsck-prepared.txt)\n' "$WORKSPACE"
    exit 1
fi

timeout "$((TIMEOUT + 30))" python3 "$CLIENT" "$CONSOLE_LOG" "$SERIAL_SOCK" \
    "$MONITOR_SOCK" "$PIDFILE" "$TIMEOUT" &
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

if [[ $CLIENT_STATUS -ne 0 ]]; then
    printf 'FAIL (the audio session did not complete; see %s)\n' "$CONSOLE_LOG"
    exit 1
fi
for marker in 'AUDIO_APT=0' 'AUDIO_APLAY=0' 'AUDIO_CHECK_DONE'; do
    if ! grep -aqF -- "$marker" "$CONSOLE_LOG"; then
        printf 'FAIL (console is missing "%s")\n' "$marker"
        exit 1
    fi
done

if ! python3 - "$CAPTURE" <<'PY'
import struct
import sys

data = open(sys.argv[1], "rb").read()
if len(data) < 44 or data[:4] != b"RIFF":
    print("FAIL (the audio backend captured nothing)")
    raise SystemExit(1)
# The driver plays each aplay chunk synchronously, so the capture has short
# silences between chunks: verify the tone itself rather than its wall-clock
# continuity.
pcm = data[44:44 + 96000 * 4]
samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm[: len(pcm) // 2 * 2])
if not samples:
    print("FAIL (the captured stream is empty)")
    raise SystemExit(1)
peak = max(abs(s) for s in samples)
nonzero = sum(1 for sample in samples if sample != 0)
changes = 0
for index in range(1, len(samples)):
    if samples[index - 1] != 0 and samples[index] != 0:
        if (samples[index - 1] < 0) != (samples[index] < 0):
            changes += 1
print("capture peak=%d tone_samples=%d sign_changes=%d" % (peak, nonzero, changes))
if peak != 8000 or nonzero < 20000 or changes < 100:
    print("FAIL (the captured audio is not the injected tone)")
    raise SystemExit(1)
PY
then
    printf 'FAIL (captured samples do not match the tone)\n'
    exit 1
fi

if ! e2fsck -fn "$IMAGE" >"$WORKSPACE/fsck-after.txt" 2>&1; then
    printf 'FAIL (e2fsck -fn; see %s/fsck-after.txt)\n' "$WORKSPACE"
    exit 1
fi

printf 'PASS (aplay -D hw:0,0 through /dev/snd/pcmC0D0p, captured tone verified)\n'
