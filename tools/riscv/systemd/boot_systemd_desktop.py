#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the SYSTEMD-DESKTOP-M1 initramfs on Asterinas RISC-V and report progress.

Drives the same QEMU ``-machine virt`` / U-Boot ``booti`` handoff as the other
RISC-V runners, *with* the bochs-display framebuffer chain (U-Boot injects a
``simple-framebuffer`` DTB node so the kernel registers ``/dev/fb0``) so that
Xorg has a device to run on. The initramfs ``/init`` exec()s systemd as PID 1;
systemd then reaches ``graphical.target`` and starts ``xorg.service`` + the
desktop session services.

Because systemd is a long-running PID 1 it never exits; the driver collects
until the terminal milestones appear or the collection timeout elapses, then
dumps the transcript tail and reports each milestone.

Usage:
    python3 tools/riscv/systemd/boot_systemd_desktop.py \
        [--serial-log /tmp/sd-serial.log] [--collect-timeout 300] [--screenshot /tmp/sd.ppm]
"""

from __future__ import annotations

import argparse
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

REPO = Path(__file__).resolve().parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"
MON_SOCK = Path("/tmp/systemd-desktop-mon.sock")

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"

# Terminal milestone: the desktop is up when systemd reports graphical.target
# AND Xorg has finished input-device bring-up.
GRAPHICAL_TARGET = b"Reached target Graphical Interface"
XORG_BANNER = b"X.Org X Server"
XORG_INPUT = b'Adding extended input device'
EMERGENCY = b"Welcome to emergency mode"

# Progress markers we report on (none of these end collection on its own).
VERSION_BANNER = b"running in system mode"
ARCH_LINE = b"Detected architecture riscv64"
BASIC_TARGET = b"Reached target Basic System"
MULTIUSER_TARGET = b"Reached target Multi-User System"
XORG_STARTED = b"Started Xorg display server"
WM_STARTED = b"Started Matchbox window manager"
PANEL_STARTED = b"Started Xpanel"
PCMANFM_STARTED = b"Started PCManFM file manager"
XTERM_STARTED = b"Started XTerm terminal emulator"

PANIC_MARKERS = [
    b"kernel panic", b"Kernel panic", b"page fault handler failed",
    b"Oops", b"BUG:", b"panic!",
]


def uboot_commands() -> list[tuple[str, str, str]]:
    """U-Boot command sequence: booti handoff + bochs framebuffer DTB injection."""
    return [
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


def screendump(sock: Path, path: Path) -> None:
    if not sock.exists():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(sock))
            s.settimeout(5)
            s.sendall(f"screendump {path}\n".encode())
            time.sleep(2.0)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path, default=Path("/tmp/asterinas-sd-desktop.log"))
    parser.add_argument("--collect-timeout", type=float, default=300.0)
    parser.add_argument("--screenshot", type=Path, default=Path("/tmp/asterinas-sd-desktop.ppm"))
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}")
    if MON_SOCK.exists():
        MON_SOCK.unlink()

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "1",
        "-display", "none",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "bochs-display",
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
        "-serial", "stdio",
        "-monitor", f"unix:{MON_SOCK},server,nowait",
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

        # systemd runs forever; collect until the desktop is up or timeout.
        print(f"[boot] collecting systemd+Xorg output (timeout={args.collect_timeout}s)", flush=True)
        deadline = time.monotonic() + args.collect_timeout
        while time.monotonic() < deadline:
            # systemd colorizes unit names in its status lines (e.g. the target
            # name inside "Reached target …"), so match against ANSI-stripped
            # bytes — the raw pending buffer has escapes in the middle.
            clean_pending = ANSI_RE.sub(b"", bytes(boot.pending))
            if GRAPHICAL_TARGET in clean_pending and XORG_INPUT in clean_pending:
                reached = "desktop-up"
                break
            if EMERGENCY in clean_pending:
                reached = "emergency"
                break
            if any(m in clean_pending for m in PANIC_MARKERS):
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
        screendump(MON_SOCK, args.screenshot)
        boot.close()

    raw = bytes(boot.transcript)
    clean = ANSI_RE.sub(b"", raw)  # systemd colorizes unit names; strip for matching
    transcript = clean.decode("utf-8", "replace")
    markers = {
        "init-launcher": INIT_MARKER in clean,
        "systemd-banner": VERSION_BANNER in clean,
        "arch-riscv64": ARCH_LINE in clean,
        "basic-target": BASIC_TARGET in clean,
        "multi-user-target": MULTIUSER_TARGET in clean,
        "graphical-target": GRAPHICAL_TARGET in clean,
        "xorg-started": XORG_STARTED in clean,
        "xorg-banner": XORG_BANNER in clean,
        "xorg-input-devices": XORG_INPUT in clean,
        "matchbox-started": WM_STARTED in clean,
        "xpanel-started": PANEL_STARTED in clean,
        "pcmanfm-started": PCMANFM_STARTED in clean,
        "xterm-started": XTERM_STARTED in clean,
        "emergency": EMERGENCY in clean,
    }
    panics = [m.decode() for m in PANIC_MARKERS if m in clean]
    unimplemented = transcript.count("Unimplemented syscall")

    print("\n=== SYSTEMD-DESKTOP-M1 result ===", flush=True)
    for k, v in markers.items():
        print(f"  {k}: {'OK' if v else 'MISSING'}", flush=True)
    print(f"  collection-ended: {reached}", flush=True)
    print(f"  unimplemented-syscall lines: {unimplemented}", flush=True)
    if panics:
        print(f"  panic markers: {panics}", flush=True)
    if args.screenshot.exists():
        print(f"  screenshot: {args.screenshot}", flush=True)

    print("\n=== serial tail ===", flush=True)
    print(transcript[-6000:], flush=True)

    # Success: systemd reached graphical.target and Xorg brought up its input
    # devices (i.e. the desktop session is running under systemd).
    ok = markers["graphical-target"] and markers["xorg-started"] and markers["xorg-banner"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
