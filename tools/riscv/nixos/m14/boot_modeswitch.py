#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""DRM-M14 KMS mode-switch smoke driver.

Boots the minimal modeswitch disk (a single static /init that drives
DRM_IOCTL_MODE_SETCRTC between two resolutions on /dev/dri/card0) under
virtio-gpu, then reports the ``[MODESWITCH] ...`` lines and the pass/fail marker
captured from the serial console.

Exit 0 iff the guest emitted ``__MODESWITCH_DONE__ __MODESWITCH_PASS__``.
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

ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

DISK = Path("/tmp/m14-modeswitch")
UBOOT = DISK / "u-boot"
BOOT_DISK = DISK / "boot.ext4"
MON_SOCK = DISK / "mon.sock"

CPU = "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true"
KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x9000_0000

DONE = b"__MODESWITCH_DONE__"
PASS = b"__MODESWITCH_PASS__"
FAIL = b"__MODESWITCH_FAIL__"
PANIC = [b"kernel panic", b"Kernel panic", b"panic!", b"page fault handler failed",
         b"Oops", b"BUG:"]


class Boot:
    def __init__(self, argv, serial_log: Path):
        serial_log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = serial_log.open("wb")
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
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
                    f"timed out waiting for {needle!r}; tail={self.transcript[-800:]!r}")
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


def uboot_commands() -> list[tuple[str, str, str]]:
    return [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=info init=/init"', "=>"),
        ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio", "bytes read"),
        ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
        ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
         "Starting kernel ..."),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path, default=DISK / "serial.log")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    for p in (UBOOT, BOOT_DISK):
        if not p.exists():
            raise SystemExit(f"missing {p} — run build_modeswitch.sh first")
    if MON_SOCK.exists():
        MON_SOCK.unlink()

    argv = [
        "qemu-system-riscv64", "-machine", "virt", "-cpu", CPU,
        "-m", "2G", "-smp", "1", "-display", "none", "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-gpu-device",
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
        "-serial", "stdio", "-monitor", f"unix:{MON_SOCK},server,nowait",
    ]

    boot = Boot(argv, args.serial_log)
    reached = "timeout"
    try:
        print("[boot] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)
        for name, text, expected in uboot_commands():
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        print(f"[boot] waiting for modeswitch result (timeout={args.timeout}s)", flush=True)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            clean = ANSI_RE.sub(b"", bytes(boot.pending))
            if DONE in clean:
                reached = "pass" if PASS in clean else "fail"
                break
            if any(m in clean for m in PANIC):
                reached = "panic"
                break
            try:
                boot._drain(1.0)
            except RuntimeError:
                reached = "serial-closed"
                break
    except TimeoutError as e:
        reached = "timeout"
        print(f"[boot] {e}", flush=True)
    finally:
        boot.close()

    clean = ANSI_RE.sub(b"", bytes(boot.transcript))
    transcript = clean.decode("utf-8", "replace")

    print(f"\n=== DRM-M14 modeswitch result ({reached}) ===", flush=True)
    for l in transcript.splitlines():
        if "[MODESWITCH]" in l or "__MODESWITCH" in l:
            print(f"  {l}", flush=True)

    print("\n=== serial tail ===", flush=True)
    print(transcript[-1500:], flush=True)

    ok = reached == "pass"
    print(f"\n=== DRM-M14 modeswitch: {'PASS' if ok else 'FAIL'} ===", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
