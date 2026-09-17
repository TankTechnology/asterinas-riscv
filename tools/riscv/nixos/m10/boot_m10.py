#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""DRM-M10 desktop boot + xbench benchmark driver.

Boots the M10 desktop disk (kernel + initramfs + correct 4-CPU/2G DTB) with
either the virtio-gpu (modesetting, --gpu drm) or bochs (fbdev, --gpu bochs)
display, waits for the xbench oneshot service to finish, and prints the
XBENCH ops/sec lines captured from the serial console.

Exit 0 iff systemd reached graphical.target, Xorg started, and xbench ran.
"""

from __future__ import annotations

import argparse
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

REPO = Path("/home/arch-anjie/Program/asterinas-riscv-drm")
OUT = Path("/tmp/m10-desktop")
UBOOT = OUT / "u-boot"
BOOT_DISK = OUT / "boot.ext4"
MON_SOCK = OUT / "mon.sock"
KERNEL_IMAGE = REPO / "target/osdk/aster-kernel/aster-kernel-osdk-bin.Image"
INITRD_CPIO = REPO / "target/nixos/m10/initramfs.cpio"

CPU = "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true"


def gen_dtb(smp: int) -> Path:
    """Regenerate the qemu-virt DTB with a memory node and CPU count that
    EXACTLY match the QEMU -m / -smp flags used to boot (a mismatched DTB is
    what broke smp=4 in M8/M9)."""
    dtb = OUT / "qemu-virt.dtb"
    subprocess.run(
        ["qemu-system-riscv64", "-machine", "virt", "-m", "2G", "-smp", str(smp),
         "-cpu", CPU, "-machine", f"dumpdtb={dtb}", "-nographic"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return dtb


def pack_disk(smp: int) -> None:
    stage = OUT / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy(KERNEL_IMAGE, stage / "asterinas.booti")
    shutil.copy(INITRD_CPIO, stage / "initramfs.cpio")
    shutil.copy(gen_dtb(smp), stage / "qemu-virt.dtb")
    ib = INITRD_CPIO.stat().st_size
    kb = KERNEL_IMAGE.stat().st_size
    mb = max(256, (ib + kb + 32 * 1024 * 1024) // 1024 // 1024 + 1)
    BOOT_DISK.unlink(missing_ok=True)
    with open(BOOT_DISK, "wb") as f:
        f.truncate(mb * 1024 * 1024)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-d", str(stage), str(BOOT_DISK)],
                   check=True)
    shutil.rmtree(stage, ignore_errors=True)

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x9000_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
GRAPHICAL_TARGET = b"Reached target Graphical Interface"
XORG_BANNER = b"X.Org X Server"
XORG_STARTED = b"Started Xorg display server"
XORG_INPUT = b"Adding extended input device"
MODESET_DRIVER = b"Loading /usr/lib/xorg/modules/drivers/modesetting_drv.so"
MODESET_USING = b"modeset(0): using default device"
FBDEV_DRIVER = b"Loading /usr/lib/xorg/modules/drivers/fbdev_drv.so"
XBENCH_DONE = b"XBENCH done"
PANIC_MARKERS = [b"kernel panic", b"Kernel panic", b"page fault handler failed",
                 b"Oops", b"BUG:", b"panic!"]


def uboot_commands(gpu: str) -> list[tuple[str, str, str]]:
    cmds = [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
    ]
    if gpu == "bochs":
        cmds += [
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
        ]
    cmds += [
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
        ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio", "bytes read"),
        ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
        ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
         "Starting kernel ..."),
    ]
    return cmds


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path, default=OUT / "serial.log")
    parser.add_argument("--gpu", choices=["drm", "bochs"], default="drm")
    parser.add_argument("--collect-timeout", type=float, default=420.0)
    parser.add_argument("--init-timeout", type=float, default=300.0)
    parser.add_argument("--smp", type=int, default=1)
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not INITRD_CPIO.exists():
        raise SystemExit(f"missing initramfs: {INITRD_CPIO} — run build_m10_desktop.sh first")
    if MON_SOCK.exists():
        MON_SOCK.unlink()

    pack_disk(args.smp)

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", "none",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
    ]
    if args.gpu == "drm":
        argv += ["-device", "virtio-gpu-device"]
    else:
        argv += ["-device", "bochs-display"]
    argv += ["-serial", "stdio", "-monitor", f"unix:{MON_SOCK},server,nowait"]

    boot = Boot(argv, args.serial_log)
    reached = "timeout"
    try:
        print("[boot] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)
        for name, text, expected in uboot_commands(args.gpu):
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        print("[boot] waiting for /init launcher", flush=True)
        boot.read_until(INIT_MARKER, args.init_timeout)
        print("[ok] /init reached (exec'ing systemd)", flush=True)

        print(f"[boot] collecting (timeout={args.collect_timeout}s)", flush=True)
        deadline = time.monotonic() + args.collect_timeout
        while time.monotonic() < deadline:
            clean = ANSI_RE.sub(b"", bytes(boot.pending))
            if GRAPHICAL_TARGET in clean and XORG_INPUT in clean and XBENCH_DONE in clean:
                reached = "xbench-done"
                break
            if any(m in clean for m in PANIC_MARKERS):
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
    except RuntimeError as e:
        reached = "serial-closed"
        print(f"[boot] serial closed early: {e}", flush=True)
    finally:
        boot.close()

    clean = ANSI_RE.sub(b"", bytes(boot.transcript))
    transcript = clean.decode("utf-8", "replace")

    print(f"\n=== DRM-M10 xbench result (gpu={args.gpu}, smp={args.smp}) ===", flush=True)
    print(f"  collection-ended: {reached}", flush=True)
    print("  --- XBENCH ---", flush=True)
    xbench_lines = [l for l in transcript.splitlines() if "XBENCH" in l]
    for l in xbench_lines:
        print(f"  {l}", flush=True)

    print("\n=== serial tail ===", flush=True)
    print(transcript[-2500:], flush=True)

    ok = reached == "xbench-done" and len(xbench_lines) >= 8
    print(f"\n=== DRM-M10 desktop: {'PASS' if ok else 'FAIL'} (gpu={args.gpu}, smp={args.smp}) ===",
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
