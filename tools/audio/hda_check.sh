#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# HDA audio gate.
#
# Boots the kernel with QEMU's `intel-hda` controller and an `hda-output`
# codec whose audio backend is a WAV file, waits for the driver's playback
# self-test, and checks the captured samples: a 440 Hz square wave at 48 kHz
# stereo 16-bit, amplitude 8000.

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: hda_check.sh --kernel ELF --workspace DIR [OPTIONS]

Options:
  --kernel PATH     Asterinas x86_64 multiboot2 ELF (required).
  --workspace DIR   Initramfs, console transcript and captured WAV (required).
  --probe PATH      Probe source for the guest init (default:
                    tools/audio/audio_probe.c).
  --timeout SECS    Deadline for boot and playback (default: 120).
  --loglevel N      Kernel log level (default: 7, so the self-test log is kept).
  --accel MODE      'kvm' or 'tcg' (default: kvm).
  --qemu PATH       QEMU binary (default: qemu-system-x86_64).

Exit status is 0 only if the driver reports the self-test, the host captured
samples, and the samples are the expected tone.
USAGE
}

die_usage() {
    printf 'error: %s\n' "$1" >&2
    usage >&2
    exit 2
}

KERNEL=""
WORKSPACE=""
PROBE=""
TIMEOUT=120
LOGLEVEL=7
ACCEL="kvm"
QEMU="qemu-system-x86_64"

while (( $# > 0 )); do
    case "$1" in
        --kernel | --workspace | --probe | --timeout | --loglevel | --accel | --qemu)
            [[ $# -ge 2 && -n "$2" ]] || die_usage "missing value for $1"
            case "$1" in
                --kernel) KERNEL="$2" ;;
                --workspace) WORKSPACE="$2" ;;
                --probe) PROBE="$2" ;;
                --timeout) TIMEOUT="$2" ;;
                --loglevel) LOGLEVEL="$2" ;;
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
PROBE="${PROBE:-$SCRIPT_DIR/audio_probe.c}"

mkdir -p -- "$WORKSPACE"
CONSOLE="$WORKSPACE/console.txt"
WAV="$WORKSPACE/audio.wav"
QEMU_LOG="$WORKSPACE/qemu.log"
rm -f -- "$CONSOLE" "$WAV"

INITRAMFS="$(tools/drm/build_probe.sh --arch amd64 --source "$PROBE" --output "$WORKSPACE/probe")"

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
    -device intel-hda \
    -device hda-output,audiodev=audio0 \
    -audiodev "wav,id=audio0,path=$WAV" \
    -kernel "$KERNEL" \
    -initrd "$INITRAMFS" \
    -append "console=hvc0 loglevel=$LOGLEVEL init=/init aster_audio.selftest" \
    >"$QEMU_LOG" 2>&1 &
QEMU_PID=$!

cleanup() {
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
}
trap cleanup EXIT

STATUS=timeout
for _ in $(seq 1 "$TIMEOUT"); do
    if grep -aq "self-test tone finished" "$CONSOLE" 2>/dev/null; then
        STATUS=pass
        break
    fi
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        STATUS=exited
        break
    fi
    sleep 1
done

# Let the backend flush the last frames.
sleep 2
cleanup
trap - EXIT

if [[ "$STATUS" != "pass" ]]; then
    printf 'FAIL (the driver did not finish the self-test; see %s)\n' "$CONSOLE"
    exit 1
fi

if ! python3 - "$WAV" <<'PY'
import struct
import sys

data = open(sys.argv[1], "rb").read()
if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
    print("FAIL (the audio backend produced no WAV)")
    raise SystemExit(1)

pcm = data[44:44 + 48000 * 4]  # one second of 48 kHz stereo 16-bit
samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm[: len(pcm) // 2 * 2])
if not samples:
    print("FAIL (the captured stream is empty)")
    raise SystemExit(1)

peak = max(abs(sample) for sample in samples)
changes = sum(
    1
    for index in range(1, len(samples))
    if (samples[index - 1] < 0) != (samples[index] < 0)
)
print("capture peak=%d sign_changes=%d" % (peak, changes))
# The self-test tone: 440 Hz square wave, amplitude 8000.
if peak != 8000:
    print("FAIL (unexpected peak amplitude)")
    raise SystemExit(1)
if not (700 <= changes <= 1060):
    print("FAIL (the captured wave is not ~440 Hz)")
    raise SystemExit(1)
PY
then
    printf 'FAIL (the captured audio is not the self-test tone)\n'
    exit 1
fi

printf 'PASS (HDA playback: controller, codec 2/3, 48 kHz stereo tone captured)\n'
