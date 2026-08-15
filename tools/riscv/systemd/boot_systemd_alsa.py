#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""POLISH-M7 driver: verify `aplay` produces a tone inside the full systemd system.

Boots the systemd+ALSA initramfs with a ``virtio-sound-device`` wired to a QEMU
``wav`` backend, logs in over getty@ttyS0, and runs the Alpine prebuilt
``aplay -D hw:0,0 /sine.wav`` in the interactive root shell. It then decodes the
host-side WAV and asserts the tone is audible (amplitude + pitch), proving audio
works through the complete systemd stack — not just the minimal busybox
initramfs that AUDIO-M2/POLISH-M6 used.

Same QEMU virt / U-Boot ``booti`` handoff and getty/login flow as
boot_systemd_m2.py, plus the ``-audiodev``/``virtio-sound`` devices from the
ALSA harness.
"""

from __future__ import annotations

import argparse
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nixos" / "audio"))
import boot_audio  # noqa: E402  (verify_tone + Boot helpers)

ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

REPO = Path(__file__).resolve().parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
LOGIN_PROMPT = b"login:"
SHELL_READY = b"___LOGIN_SHELL_READY___"
ALSA_OK = b"M7_ALSA_OK"
ALSA_FAIL = b"M7_ALSA_FAIL"

PANIC_MARKERS = [
    b"kernel panic", b"Kernel panic", b"page fault handler failed",
    b"Oops", b"BUG:", b"panic!",
]


def uboot_commands() -> list[tuple[str, str, str]]:
    return [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
        ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
        ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
        ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
         "Starting kernel ..."),
    ]


class Boot:
    """Minimal serial driver (same contract as boot_systemd_m2.Boot)."""

    def __init__(self, argv: list[str], serial_log: Path) -> None:
        serial_log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = serial_log.open("wb")
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        self.pending = bytearray()
        self.transcript = bytearray()

    def _drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for key, _ in self.sel.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial process closed output")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)

    def read_until(self, needle: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for {needle!r}; tail={bytes(self.transcript[-800:])!r}"
                )
            self._drain(min(remaining, 1.0))
        idx = self.pending.index(needle)
        end = idx + len(needle)
        consumed = bytes(self.pending[:end])
        del self.pending[:end]
        return consumed

    def send(self, text: str) -> None:
        self.proc.stdin.write((text + "\n").encode())
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=5)
        except ProcessLookupError:
            pass
        self.sel.close()
        self.log_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path,
                        default=Path("/tmp/asterinas-sd-alsa-serial.log"))
    parser.add_argument("--wav", type=Path,
                        default=Path("/tmp/asterinas-sd-alsa-out.wav"))
    parser.add_argument("--collect-timeout", type=float, default=240.0)
    parser.add_argument("--command-timeout", type=float, default=120.0)
    parser.add_argument("--smp", type=int, default=1)
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}")

    if args.wav.exists():
        args.wav.unlink()

    argv = [
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

    boot = Boot(argv, args.serial_log)
    results: list[tuple[str, bool]] = []
    ended = "ok"
    try:
        print("[boot] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)

        for name, text, expected in uboot_commands():
            print(f"[uboot] {name}", flush=True)
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        print("[boot] waiting for /init launcher", flush=True)
        boot.read_until(INIT_MARKER, 120)
        print("[ok] /init reached", flush=True)

        print("[getty] waiting for login prompt", flush=True)
        boot.read_until(LOGIN_PROMPT, args.collect_timeout)
        print("[ok] getty login prompt seen", flush=True)
        results.append(("getty-login-prompt", True))

        boot.send("root")
        boot.read_until(SHELL_READY, 60)
        print("[ok] root login shell ready", flush=True)
        results.append(("login-shell", True))

        # Sanity: the ALSA device nodes should be present under devtmpfs.
        boot.send("ls -l /dev/snd && echo M7_SND_NODES")
        try:
            boot.read_until(b"M7_SND_NODES", 30)
            results.append(("snd-nodes", True))
        except TimeoutError:
            results.append(("snd-nodes", False))
        boot._drain(0.5)

        # The headline probe: unmodified musl aplay against virtio-sound.
        print("[probe] aplay", flush=True)
        cmd = "aplay -D hw:0,0 /sine.wav && echo M7_ALSA_OK || echo M7_ALSA_FAIL"
        boot.send(cmd)
        try:
            boot.read_until(cmd.encode(), 15)
        except TimeoutError:
            pass  # echo may have been split/partial
        try:
            boot.read_until(ALSA_OK, args.command_timeout)
            results.append(("aplay", True))
        except TimeoutError:
            boot.read_until(ALSA_FAIL, 10)
            results.append(("aplay", False))
        boot._drain(1.0)
    except TimeoutError as e:
        ended = "timeout"
        print(f"[boot] {e}", flush=True)
    except RuntimeError:
        ended = "serial-closed"
    finally:
        boot.close()

    raw = bytes(boot.transcript)
    clean = ANSI_RE.sub(b"", raw)
    transcript = clean.decode("utf-8", "replace")
    panics = [m.decode() for m in PANIC_MARKERS if m in clean]
    unimplemented = transcript.count("Unimplemented syscall")

    print("\n=== SYSTEMD-ALSA guest result ===", flush=True)
    for name, ok in results:
        print(f"  {name}: {'OK' if ok else 'FAIL'}", flush=True)
    print(f"  collection-ended: {ended}", flush=True)
    print(f"  unimplemented-syscall lines: {unimplemented}", flush=True)
    if panics:
        print(f"  panic markers: {panics}", flush=True)

    # Host-side: decode the WAV and verify amplitude + pitch.
    print("\n=== SYSTEMD-ALSA audible-tone verification ===", flush=True)
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
    print(f"  audible tone : {'OK' if tone_ok else 'FAIL'}", flush=True)

    aplay_ok = any(name == "aplay" and ok for name, ok in results)
    result = aplay_ok and tone_ok
    print(f"\n=== SYSTEMD-ALSA: {'PASS' if result else 'FAIL'} (smp={args.smp}) ===", flush=True)

    if not result:
        print("\n=== serial tail ===", flush=True)
        print(transcript[-4000:], flush=True)

    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
