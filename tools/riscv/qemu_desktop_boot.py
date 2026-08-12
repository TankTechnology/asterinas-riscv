#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot Asterinas RISC-V on QEMU virt via U-Boot with a bochs framebuffer.

This drives a QEMU ``-machine virt`` guest through the full firmware-framebuffer
handoff chain:

    QEMU bochs-display -> U-Boot (CONFIG_VIDEO_BOCHS) initializes the display
        -> U-Boot injects a ``simple-framebuffer`` DTB node
        -> Asterinas parses it (ostd simple_framebuffer)
        -> the framebuffer component adopts it and the VT console renders.

Requires the kernel to be built for Sv39 (``FEATURES=riscv_sv39_mode``), the
``generic-sv39`` U-Boot boot disk prepared by ``prepare_qemu_uboot_booti.sh``,
and a U-Boot with ``CONFIG_VIDEO_BOCHS=y`` (the pinned commit's
``qemu-riscv64_smode_defconfig`` already enables it).

Usage:
    python3 tools/riscv/qemu_desktop_boot.py            # headless + screendump
    python3 tools/riscv/qemu_desktop_boot.py --display-gtk  # open a window
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"
MON_SOCK = Path("/tmp/qemu-desktop-mon.sock")
SERIAL_SOCK = Path("/tmp/qemu-desktop-serial.sock")
SERIAL_LOG = Path("/tmp/asterinas-desktop-serial.log")
SCREENSHOT = Path("/tmp/asterinas-desktop.ppm")

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

# The bochs-display BAR0 (16 MiB VRAM) as assigned by U-Boot's PCI enumeration
# on QEMU virt. Verified via ``pci display 0.1.0``.
FB_BAR = 0x4000_0000

COMMANDS = [
    ("version", "version", "=> "),
    ("virtio-scan", "virtio scan", "=> "),
    ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
    ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
    ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
    ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
    ("dtb-resize", "fdt resize 0x1000", "=> "),
    ("pci-probe", "pci display 0.1.0", "=> "),
    ("fb-mknode", "fdt mknode / framebuffer@40000000", "=> "),
    ("fb-compatible", 'fdt set /framebuffer@40000000 compatible "simple-framebuffer"', "=> "),
    ("fb-reg", "fdt set /framebuffer@40000000 reg <0x0 0x40000000 0x0 0x1000000>", "=> "),
    ("fb-width", "fdt set /framebuffer@40000000 width <0x500>", "=> "),     # 1280
    ("fb-height", "fdt set /framebuffer@40000000 height <0x400>", "=> "),    # 1024
    ("fb-stride", "fdt set /framebuffer@40000000 stride <0x1400>", "=> "),   # 1280*4 = 5120
    ("fb-format", 'fdt set /framebuffer@40000000 format "x8r8g8b8"', "=> "),
    ("fb-status", 'fdt set /framebuffer@40000000 status "okay"', "=> "),
    ("fb-verify", "fdt print /framebuffer@40000000", "simple-framebuffer"),
    ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=> "),
    ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
    ("initrd-size-save", "setenv initrd_size ${filesize}", "=> "),
    ("initrd-size", "echo ASTER_INITRD_SIZE=${initrd_size}", "ASTER_INITRD_SIZE="),
    ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}", "Starting kernel ..."),
]


def screendump(mon_sock: Path, path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(mon_sock))
        s.settimeout(5)
        s.sendall(f"screendump {path}\n".encode())
        time.sleep(2.0)  # allow the dump to be written


def main() -> int:
    display_mode = "gtk" if "--display-gtk" in sys.argv else "none"
    for sock in (MON_SOCK, SERIAL_SOCK):
        if sock.exists():
            sock.unlink()
    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot binary: {UBOOT}\n"
                         "run prepare_qemu_uboot_booti.sh first")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}\n"
                         "run prepare_qemu_uboot_booti.sh first")

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "1",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "bochs-display",
        "-device", "virtio-keyboard-device",
        "-display", display_mode,
        "-serial", "stdio",
    ]
    if display_mode == "none":
        argv += ["-monitor", f"unix:{MON_SOCK},server,nowait"]

    log_file = open(SERIAL_LOG, "wb")
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    os.set_blocking(proc.stdout.fileno(), False)

    output = bytearray()
    lock = threading.Lock()

    def reader() -> None:
        while True:
            try:
                chunk = os.read(proc.stdout.fileno(), 4096)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            except OSError:
                break
            if not chunk:
                break
            with lock:
                output.extend(chunk)
            log_file.write(chunk)
            log_file.flush()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    consumed = 0

    def wait_for_fresh(needle: bytes, deadline: float) -> bytes:
        """Wait for a needle that appears after the current buffer position."""
        nonlocal consumed
        while time.monotonic() < deadline:
            with lock:
                buf = bytes(output)
            if needle in buf[consumed:]:
                return buf
            time.sleep(0.1)
        raise TimeoutError(
            f"timed out waiting for {needle!r}; tail: {bytes(output)[-400:]!r}"
        )

    def advance_to(needle: bytes, deadline: float) -> None:
        """Consume output up to and including needle, returning fresh."""
        nonlocal consumed
        wait_for_fresh(needle, deadline)
        with lock:
            buf = bytes(output)
        consumed = buf.index(needle, consumed) + len(needle)

    try:
        advance_to(b"=> ", time.monotonic() + 30)
        for name, text, expected in COMMANDS:
            print(f"[send] {name}: {text}", flush=True)
            proc.stdin.write((text + "\n").encode())
            proc.stdin.flush()
            if name == "booti":
                advance_to(b"Starting kernel ...", time.monotonic() + 60)
                try:
                    advance_to(b">>> Hello from RISC-V userspace on Asterinas! <<<", time.monotonic() + 120)
                    print("[ok] userspace marker reached", flush=True)
                except TimeoutError:
                    print("[warn] userspace marker not reached", flush=True)
                # Early dump to catch the diagnostic rects (drawn ~1s after marker).
                if display_mode == "none" and MON_SOCK.exists():
                    time.sleep(1)
                    screendump(MON_SOCK, SCREENSHOT.with_name("early.ppm"))
                    print("[ok] early screenshot written", flush=True)
                    time.sleep(7)
                else:
                    time.sleep(8)
                break
            advance_to(expected.encode(), time.monotonic() + 25)
            if expected != "=> ":
                advance_to(b"=> ", time.monotonic() + 25)

        if display_mode == "none" and MON_SOCK.exists():
            screendump(MON_SOCK, SCREENSHOT)
            print(f"[ok] screenshot written to {SCREENSHOT}", flush=True)
        else:
            # In gtk display mode, keep QEMU alive so the user can watch.
            print("[ok] desktop running in a window; Ctrl+C in this script to quit", flush=True)
            try:
                while True:
                    time.sleep(1)
                    if proc.poll() is not None:
                        break
            except KeyboardInterrupt:
                pass
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
