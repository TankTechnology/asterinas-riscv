#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the SYSTEMD-BOOT-M1 initramfs on Asterinas RISC-V and report progress.

Drives the same QEMU ``-machine virt`` / U-Boot ``booti`` handoff as the other
RISC-V runners. The initramfs ``/init`` exec()s systemd as PID 1; this driver
boots to that point, collects the serial transcript, and reports how far
systemd got by scanning for its banner, the "Reached target …" lines, the
emergency-shell fallback, and any kernel panic / unimplemented-syscall noise.

Because systemd is a long-running PID 1 it never exits; the driver collects
until one of the terminal milestones appears or the collection timeout elapses,
then dumps the transcript tail and exits 0/1.

Usage:
    python3 tools/riscv/systemd/boot_systemd_smoke.py \
        [--serial-log /tmp/sd-serial.log] [--collect-timeout 120] [--smp 1]
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

REPO = Path(__file__).resolve().parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"

# Terminal milestones (whichever appears first ends collection).
BASIC_TARGET = b"Reached target Basic System"
EMERGENCY = b"Welcome to emergency mode"
SYSINIT_TARGET = b"Reached target System Initialization"

# Version / progress markers we report on (none of these end collection on its own).
VERSION_BANNER = b"running in system mode"
ARCH_LINE = b"Detected architecture riscv64"

# Failure signatures to surface in the tail dump.
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
                    f"timed out waiting for {needle!r}; tail={self.transcript[-800:]!r}"
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
    parser.add_argument("--serial-log", type=Path, default=Path("/tmp/asterinas-sd-serial.log"))
    parser.add_argument("--collect-timeout", type=float, default=120.0)
    parser.add_argument("--smp", type=int, default=1)
    parser.add_argument("--boot-disk", type=Path, default=BOOT_DISK,
                        help="boot disk image (defaults to the shared target/qemu-uboot disk)")
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not args.boot_disk.exists():
        raise SystemExit(f"missing boot disk: {args.boot_disk}")

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
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
    ]

    boot = Boot(argv, args.serial_log)
    reached = "timeout"
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
        print("[ok] /init reached (exec'ing systemd)", flush=True)

        # systemd runs forever; collect until a milestone or timeout.
        print(f"[boot] collecting systemd output (timeout={args.collect_timeout}s)", flush=True)
        deadline = time.monotonic() + args.collect_timeout
        while time.monotonic() < deadline:
            if BASIC_TARGET in boot.pending:
                reached = "basic-target"
                break
            if EMERGENCY in boot.pending:
                reached = "emergency"
                break
            if any(m in boot.pending for m in PANIC_MARKERS):
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

    raw = bytes(boot.transcript)
    clean = ANSI_RE.sub(b"", raw)  # systemd colorizes unit names; strip for matching
    transcript = clean.decode("utf-8", "replace")
    markers = {
        "init-launcher": INIT_MARKER in clean,
        "systemd-banner": VERSION_BANNER in clean,
        "arch-riscv64": ARCH_LINE in clean,
        "sysinit-target": SYSINIT_TARGET in clean,
        "basic-target": BASIC_TARGET in clean,
        "emergency": EMERGENCY in clean,
        "startup-finished": b"Startup finished" in clean,
    }
    panics = [m.decode() for m in PANIC_MARKERS if m in clean]
    unimplemented = transcript.count("Unimplemented syscall")

    print("\n=== SYSTEMD-BOOT-M1 result ===", flush=True)
    for k, v in markers.items():
        print(f"  {k}: {'OK' if v else 'MISSING'}", flush=True)
    print(f"  collection-ended: {reached}", flush=True)
    print(f"  unimplemented-syscall lines: {unimplemented}", flush=True)
    if panics:
        print(f"  panic markers: {panics}", flush=True)

    print("\n=== serial tail ===", flush=True)
    print(transcript[-4000:], flush=True)

    ok = markers["systemd-banner"] and (markers["basic-target"] or markers["emergency"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
