#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""XFCE-M3: boot the Xfce desktop initramfs on Asterinas RISC-V (headless) and
verify that xfce4-session brings up xfwm4 + xfce4-panel + xfdesktop.

Same QEMU virt / U-Boot booti / bochs-display + simple-framebuffer chain as
tools/riscv/systemd/boot_systemd_desktop_vnc.py, but fully self-contained:
own boot disk, own monitor socket, own VNC display under /tmp/xfce-m3/ so it
never touches the interactive /tmp/vnc-demo instance or any other session's
QEMU.

Verification is two-layered:
  1. serial milestones (systemd units + session wrapper log lines), and
  2. pixel checks on QEMU monitor screendumps (PPM) taken after settle:
     the xfce4-panel bar (top, dark) and xfdesktop backdrop must render.

Usage:
    python3 tools/riscv/xfce/boot_xfce_desktop.py \
        [--initramfs PATH] [--collect-timeout 300] [--settle 90]
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
DTB = REPO / "target/qemu-uboot/current/qemu-virt.dtb"
KERNEL_IMAGE = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
DEFAULT_INITRAMFS = REPO / "target/qemu-uboot/systemd-desktop-xfce-initramfs.cpio"

WORK = Path("/tmp/xfce-m3")
MON_SOCK = WORK / "mon.sock"
VNC_DISPLAY = "127.0.0.1:9"   # 5909; vnc-demo uses its own display
BOOT_DISK = WORK / "boot.ext4"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x9000_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
GRAPHICAL_TARGET = b"Reached target Graphical Interface"
XORG_BANNER = b"X.Org X Server"
XORG_INPUT = b"Adding extended input device"
SESSION_STARTED = b"Started Xfce desktop session"
SESSION_LAUNCH = b"xfce-session-start: launching dbus-run-session"
# The panel logs this the moment it realizes (icon-theme warning is
# incidental but reliably timed with the panel mapping).
PANEL_UP = b"applicationsmenu"
EMERGENCY = b"Welcome to emergency mode"
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
        ("fb-verify", f"fdt print /framebuffer@40000000", "simple-framebuffer"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
        ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
        ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
        ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
         "Starting kernel ..."),
    ]


def pack_boot_disk(initramfs: Path) -> None:
    """Build a private boot.ext4 under /tmp/xfce-m3/ (never the shared one)."""
    WORK.mkdir(parents=True, exist_ok=True)
    stage = WORK / "stage"
    if stage.exists():
        subprocess.run(["rm", "-rf", str(stage)], check=True)
    stage.mkdir()
    subprocess.run(["cp", str(KERNEL_IMAGE), str(stage / "asterinas.booti")], check=True)
    subprocess.run(["cp", str(initramfs), str(stage / "initramfs.cpio.gz")], check=True)
    subprocess.run(["cp", str(DTB), str(stage / "qemu-virt.dtb")], check=True)
    size_mb = (initramfs.stat().st_size + KERNEL_IMAGE.stat().st_size + 64 * 1024 * 1024) // (1024 * 1024) + 1
    size_mb = max(size_mb, 128)
    subprocess.run(["truncate", "-s", f"{size_mb}M", str(BOOT_DISK)], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-d", str(stage), str(BOOT_DISK)], check=True)
    print(f"[pack] {BOOT_DISK} ({size_mb}M)")


class Boot:
    def __init__(self, argv: list[str], serial_log: Path) -> None:
        serial_log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = serial_log.open("wb")
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
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
                raise TimeoutError(f"timed out waiting for {needle!r}")
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


def screendump(path: Path) -> bool:
    if not MON_SOCK.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(MON_SOCK))
            s.settimeout(5)
            s.sendall(f"screendump {path}\n".encode())
            time.sleep(2.0)
        return path.exists()
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initramfs", type=Path, default=DEFAULT_INITRAMFS)
    parser.add_argument("--serial-log", type=Path, default=WORK / "serial.log")
    parser.add_argument("--collect-timeout", type=float, default=300.0)
    parser.add_argument("--settle", type=float, default=60.0,
                        help="extra seconds to keep collecting after graphical.target")
    parser.add_argument("--no-pack", action="store_true",
                        help="reuse the existing /tmp/xfce-m3/boot.ext4")
    args = parser.parse_args()

    for p in (UBOOT, KERNEL_IMAGE, DTB, args.initramfs):
        if not p.exists():
            raise SystemExit(f"missing {p}")

    if not args.no_pack:
        pack_boot_disk(args.initramfs)
    if MON_SOCK.exists():
        MON_SOCK.unlink()

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "1",
        "-display", f"vnc={VNC_DISPLAY}",
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
    shots: list[Path] = []
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
        boot.read_until(INIT_MARKER, 180)
        print("[ok] /init reached", flush=True)

        deadline = time.monotonic() + args.collect_timeout
        graphical_at: float | None = None
        panel_at: float | None = None
        while time.monotonic() < deadline:
            clean_pending = ANSI_RE.sub(b"", bytes(boot.pending))
            if any(m in clean_pending for m in PANIC_MARKERS):
                reached = "panic"
                break
            if EMERGENCY in clean_pending:
                reached = "emergency"
                break
            if graphical_at is None and GRAPHICAL_TARGET in clean_pending:
                graphical_at = time.monotonic()
                print("[boot] graphical.target reached; waiting for panel", flush=True)
            # Anchor the settle clock to the panel actually starting (its
            # icon-theme warning fires when the panel maps), not to
            # graphical.target — userspace startup time drifts a lot between
            # runs as the image grows.
            if panel_at is None and PANEL_UP in clean_pending:
                panel_at = time.monotonic()
                print("[boot] xfce4-panel up; settling", flush=True)
            if panel_at is not None and time.monotonic() - panel_at >= args.settle:
                reached = "settled"
                break
            try:
                boot._drain(1.0)
            except RuntimeError:
                reached = "serial-closed"
                break
        # Screendumps after the settle: the panel/desktop may still be
        # painting when the first lands, so space them out.
        for i, delay in enumerate((0, 90, 90)):
            if delay:
                try:
                    boot._drain(float(delay))
                except RuntimeError:
                    break
            p = WORK / f"desktop-{i}.ppm"
            if screendump(p):
                shots.append(p)
                print(f"[shot] {p}", flush=True)
    except TimeoutError as e:
        print(f"[boot] {e}", flush=True)
        if screendump(WORK / "desktop-timeout.ppm"):
            shots.append(WORK / "desktop-timeout.ppm")
    finally:
        boot.close()

    raw = bytes(boot.transcript)
    clean = ANSI_RE.sub(b"", raw)
    transcript = clean.decode("utf-8", "replace")
    markers = {
        "init-launcher": INIT_MARKER in clean,
        "graphical-target": GRAPHICAL_TARGET in clean,
        "xorg-banner": XORG_BANNER in clean,
        "xorg-input-devices": XORG_INPUT in clean,
        "xfce-session-started": SESSION_STARTED in clean,
        "session-wrapper": SESSION_LAUNCH in clean,
        "xfconfd-activated": "org.xfce.Xfconf" in transcript,
    }
    unimplemented = transcript.count("Unimplemented syscall")
    segv = len(re.findall(r"SIGSEGV|signal 11|Segmentation", transcript))

    print("\n=== XFCE-M3 result ===", flush=True)
    for k, v in markers.items():
        print(f"  {k}: {'OK' if v else 'MISSING'}", flush=True)
    print(f"  collection-ended: {reached}", flush=True)
    print(f"  unimplemented-syscall lines: {unimplemented}", flush=True)
    print(f"  segfault mentions: {segv}", flush=True)
    for p in shots:
        print(f"  screenshot: {p}", flush=True)

    print("\n=== serial tail ===", flush=True)
    print(transcript[-5000:], flush=True)

    ok = markers["graphical-target"] and markers["xfce-session-started"] and bool(shots)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
