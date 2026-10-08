#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the ALSA smoke test (aplay against virtio-sound) and verify the tone.

Same QEMU flow as ``boot_audio.py`` (virtio-sound + ``wav`` backend), but the
guest runs the Alpine prebuilt ``aplay`` against ``/dev/snd/pcmC0D0p`` via the
ALSA ``hw:0,0`` device instead of writing raw PCM. The host verifies the PCM
actually left the guest by decoding QEMU's ``wav`` output with
:func:`boot_audio.verify_tone`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Reuse the boot/verification machinery from the AUDIO-M1 harness.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import boot_audio  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent.parent.parent
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"

DONE_MARKER = b"__ALSA_DONE__"
PASS_MARKER = b"__ALSA_PASS__"
FAIL_MARKER = b"__ALSA_FAIL__"
EXIT_RE = __import__("re").compile(rb"__ALSA_EXIT=(-?\d+)__")


def qemu_argv(args) -> list[str]:
    return [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", "none",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-audiodev", f"wav,id=audio0,path={args.wav},out.frequency=48000",
        "-device", "virtio-sound-device,audiodev=audio0",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path,
                        default=Path("/tmp/asterinas-alsa-serial.log"))
    parser.add_argument("--wav", type=Path,
                        default=Path("/tmp/asterinas-alsa-out.wav"))
    parser.add_argument("--command-timeout", type=float, default=120.0)
    parser.add_argument("--smp", type=int, default=1)
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}")

    if args.wav.exists():
        args.wav.unlink()

    boot = boot_audio.Boot(qemu_argv(args), args.serial_log)
    try:
        print("[boot] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)

        for name, text, expected in boot_audio.uboot_commands():
            print(f"[uboot] {name}", flush=True)
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        print("[boot] waiting for ALSA completion", flush=True)
        try:
            boot.read_until(DONE_MARKER, args.command_timeout)
            boot.read_until_any([PASS_MARKER, FAIL_MARKER], 60)
        except TimeoutError:
            tail = bytes(boot.transcript[-4000:])
            print("[alsa] FAIL: no completion marker (hang/crash)", flush=True)
            print(tail.decode("utf-8", "replace")[-3000:], flush=True)
            return 1
    finally:
        boot.close()

    text = bytes(boot.transcript).decode("utf-8", "replace")
    passed = PASS_MARKER in bytes(boot.transcript)

    print("\n=== ALSA guest result ===", flush=True)
    for line in text.splitlines():
        if "__ALSA" in line or line.startswith("[alsa") or "Playing WAVE" in line:
            print(line, flush=True)

    # Host-side: decode the WAV and verify amplitude + pitch.
    print("\n=== ALSA audible-tone verification ===", flush=True)
    try:
        tone = boot_audio.verify_tone(args.wav)
        tone_ok = bool(tone.get("ok"))
    except (OSError, ValueError) as exc:
        tone = {"ok": False, "error": str(exc)}
        tone_ok = False
    if "error" in tone:
        print(f"  decode error: {tone['error']}", flush=True)
    else:
        print(f"  fmt          : {tone.get('channels')} ch, {tone.get('rate')} Hz, "
              f"{tone.get('bits')}-bit, {tone.get('frames')} frames", flush=True)
        print(f"  amplitude    : RMS={tone.get('rms')}  peak={tone.get('peak')} "
              f"(min RMS {boot_audio.MIN_RMS:.0f})", flush=True)
        print(f"  pitch        : {tone.get('freq_hz')} Hz "
              f"(expect {boot_audio.TONE_FREQ:.0f} ± {boot_audio.TONE_TOL_HZ:.0f})", flush=True)
        print(f"  playable copy: {tone.get('playable')}", flush=True)
    print(f"  audible tone : {'OK' if tone_ok else 'FAIL'}", flush=True)

    result = passed and tone_ok
    print(f"\n=== ALSA: {'PASS' if result else 'FAIL'} (smp={args.smp}) ===", flush=True)
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
