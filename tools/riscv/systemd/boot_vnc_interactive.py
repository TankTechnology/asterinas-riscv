#!/usr/bin/env python3
"""Interactive VNC boot of the full Asterinas RISC-V desktop.

Same QEMU/U-Boot booti handoff as boot_systemd_desktop.py, but instead of
collecting evidence and exiting, it keeps QEMU running forever so the user can
interact through the VNC display (vnc=127.0.0.1:1).
"""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/arch-anjie/Program/asterinas-riscv")
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = Path("/tmp/vnc-demo/boot.ext4")

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x9000_0000

UBOOT_COMMANDS = [
    ("version", "version", "U-Boot 2026"),
    ("virtio-scan", "virtio scan", "=>"),
    ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
    ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
    ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
    ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
    ("dtb-resize", "fdt resize 0x1000", "=>"),
    ("pci-probe", "pci display 0.1.0", "=>"),
    ("fb-mknode", "fdt mknode / framebuffer@40000000", "=>"),
    ("fb-compatible", 'fdt set /framebuffer@40000000 compatible "simple-framebuffer"', "=>"),
    ("fb-reg", "fdt set /framebuffer@40000000 reg <0x0 0x40000000 0x0 0x1000000>", "=>"),
    ("fb-width", "fdt set /framebuffer@40000000 width <0x500>", "=>"),
    ("fb-height", "fdt set /framebuffer@40000000 height <0x400>", "=>"),
    ("fb-stride", "fdt set /framebuffer@40000000 stride <0x1400>", "=>"),
    ("fb-format", 'fdt set /framebuffer@40000000 format "x8r8g8b8"', "=>"),
    ("fb-status", 'fdt set /framebuffer@40000000 status "okay"', "=>"),
    ("fb-verify", "fdt print /framebuffer@40000000", "simple-framebuffer"),
    ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
    ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
    ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
    ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
     "Starting kernel ..."),
]


class Boot:
    def __init__(self) -> None:
        argv = [
            "qemu-system-riscv64",
            "-machine", "virt",
            "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
            "-m", "2G",
            "-smp", "1",
            "-display", "vnc=127.0.0.1:1",
            "-kernel", str(UBOOT),
            "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
            "-device", "virtio-blk-device,drive=bootdisk",
            "-device", "bochs-display",
            "-device", "virtio-keyboard-device",
            "-device", "virtio-tablet-device",
            "-serial", "stdio",
        ]
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        self.pending = bytearray()
        self.transcript = bytearray()

    def read_until(self, needle: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tail = bytes(self.transcript[-500:])
                raise TimeoutError(f"timeout waiting for {needle!r}: {tail!r}")
            for key, _ in self.sel.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("qemu closed output")
                self.transcript.extend(chunk)
                self.pending.extend(chunk)
        idx = self.pending.index(needle)
        del self.pending[: idx + len(needle)]

    def send(self, text: str) -> None:
        self.proc.stdin.write((text + "\n").encode())
        self.proc.stdin.flush()


def main() -> int:
    if not UBOOT.exists():
        print(f"missing U-Boot: {UBOOT}", file=sys.stderr)
        return 2
    if not BOOT_DISK.exists():
        print(f"missing boot disk: {BOOT_DISK}", file=sys.stderr)
        return 2

    boot = Boot()
    print("[boot] waiting for U-Boot prompt", flush=True)
    boot.read_until(b"=> ", 60)
    for name, text, expected in UBOOT_COMMANDS:
        print(f"[uboot] {name}", flush=True)
        boot.send(text)
        if name == "booti":
            boot.read_until(b"Starting kernel ...", 90)
        else:
            boot.read_until(expected.encode(), 60)
            if expected != "=>":
                boot.read_until(b"=> ", 30)
    print("[boot] booti issued — desktop will appear on VNC :1", flush=True)
    print("[boot] keepalive: reading serial output until interrupted", flush=True)

    # Keep reading serial forever (systemd+Xorg output); never exit.
    while True:
        try:
            for key, _ in boot.sel.select(2.0):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    print("[boot] qemu exited", flush=True)
                    return 1
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        except KeyboardInterrupt:
            boot.proc.terminate()
            return 0


if __name__ == "__main__":
    sys.exit(main())
