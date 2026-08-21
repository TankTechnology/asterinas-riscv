#!/usr/bin/env python3
"""Interactive GTK boot of the full Asterinas RISC-V desktop.

Same QEMU/U-Boot booti handoff as boot_systemd_desktop.py, but instead of
collecting evidence and exiting, it keeps QEMU running forever so the user can
interact through the GTK display (``-display gtk``).
"""
from __future__ import annotations

import os
import re as _re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = Path("/tmp/vnc-demo/boot.ext4")

# Current artifacts the boot disk is re-packed from (see repack_boot_disk()).
# Repacking from these — rather than trusting a long-lived /tmp/vnc-demo/boot.ext4
# — is what keeps the interactive desktop from booting a stale initramfs. A stale
# initramfs is exactly the BROWSER-M9 bug: /tmp/vnc-demo/boot.ext4 still carried a
# pre-M1 netsurf-gtk (missing the `accelerators` resource, build_netsurf.sh's
# install-gtk fix) whose netsurf.service crash-looped (status=1, Restart=always).
KERNEL_IMAGE = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
INITRD = REPO / "target/qemu-uboot/systemd-desktop-initramfs.cpio"
DTB = REPO / "target/qemu-uboot/current/qemu-virt.dtb"

# HANG-FIX: the pre-generated qemu-virt.dtb above is a 1-CPU DTB, but this
# script boots -smp 4 while QEMU's riscv virt machine picks a *random* physical
# boot hart. With a 1-hart DTB the kernel only enables PLIC sources on the
# (possibly parked) hart 0's context, so whenever the boot hart isn't hart 0
# all external IRQs are dead and the boot hangs at the first IRQ-dependent
# operation (the virtio-blk partition-table read). Dump a DTB that matches the
# actual -smp count at every launch instead.
SMP4_DTB = Path("/tmp/vnc-demo/qemu-virt-smp4.dtb")


def gen_smp4_dtb() -> Path:
    """Dump a DTB from QEMU with the same -cpu/-smp as the real boot."""
    SMP4_DTB.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["timeout", "10", "qemu-system-riscv64",
         "-machine", f"virt,dumpdtb={SMP4_DTB}",
         "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
         "-m", "2G", "-smp", "4", "-display", "none"],
        check=False, capture_output=True)
    if not SMP4_DTB.exists():
        print(f"[boot] dumpdtb failed; falling back to {DTB}", flush=True)
        return DTB
    return SMP4_DTB

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x9000_0000

def _mk_uboot_commands(fb_addr: int) -> list[tuple[str, str, str]]:
    fb_node = f"framebuffer@{fb_addr:x}"
    return [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
        ("dtb-resize", "fdt resize 0x1000", "=>"),
        ("pci-probe", "pci", "=>"),  # full PCI scan to assign BARs
        ("fb-mknode", f"fdt mknode / {fb_node}", "=>"),
        ("fb-compatible", f'fdt set /{fb_node} compatible "simple-framebuffer"', "=>"),
        ("fb-reg", f"fdt set /{fb_node} reg <0x0 {fb_addr:#x} 0x0 0x1000000>", "=>"),
        ("fb-width", f"fdt set /{fb_node} width <0x500>", "=>"),
        ("fb-height", f"fdt set /{fb_node} height <0x400>", "=>"),
        ("fb-stride", f"fdt set /{fb_node} stride <0x1400>", "=>"),
        ("fb-format", f'fdt set /{fb_node} format "x8r8g8b8"', "=>"),
        ("fb-status", f'fdt set /{fb_node} status "okay"', "=>"),
        ("fb-verify", f"fdt print /{fb_node}", "simple-framebuffer"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
        ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
        ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
        ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
         "Starting kernel ..."),
    ]


def repack_boot_disk() -> None:
    """Re-pack BOOT_DISK from the current kernel + initramfs + DTB.

    The kernel's initramfs unpacker auto-detects raw-newc vs gzip, so the raw
    systemd-desktop-initramfs.cpio is staged under the legacy `initramfs.cpio.gz`
    name the booti handoff expects (same convention as net_validate.sh). Repacking
    every launch is what keeps this interactive desktop from silently booting a
    stale initramfs (the BROWSER-M9 crash-loop root cause).
    """
    missing = [p for p in (KERNEL_IMAGE, INITRD) if not p.exists()]
    if missing:
        raise SystemExit(f"missing build artifacts: {', '.join(str(p) for p in missing)}")
    dtb = gen_smp4_dtb()
    BOOT_DISK.parent.mkdir(parents=True, exist_ok=True)
    stage = Path("/tmp/vnc-demo/repack-stage")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copy2(KERNEL_IMAGE, stage / "asterinas.booti")
    shutil.copy2(INITRD, stage / "initramfs.cpio.gz")
    shutil.copy2(dtb, stage / "qemu-virt.dtb")
    initrd_bytes = INITRD.stat().st_size
    kernel_bytes = KERNEL_IMAGE.stat().st_size
    boot_mb = (initrd_bytes + kernel_bytes + 64 * 1024 * 1024) // 1024 // 1024 + 1
    if boot_mb < 128:
        boot_mb = 128
    subprocess.run(["truncate", "-s", f"{boot_mb}M", str(BOOT_DISK)], check=True)
    subprocess.run(
        ["mkfs.ext4", "-q", "-F", "-d", str(stage), str(BOOT_DISK)], check=True)
    shutil.rmtree(stage)
    print(f"[boot] re-packed {BOOT_DISK} ({boot_mb} MB) from current kernel+initramfs",
          flush=True)


class Boot:
    def __init__(self) -> None:
        argv = [
            "qemu-system-riscv64",
            "-machine", "virt",
            "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
            "-m", "2G",
            "-smp", "4",
            "-display", "gtk",
            # slirp user networking: the kernel's hardcoded eth0 is
            # 10.0.2.15/24 (gw 10.0.2.2, DNS 10.0.2.3 via /etc/resolv.conf),
            # so NetSurf can actually fetch pages.
            "-netdev", "user,id=net0",
            "-device", "virtio-net-device,netdev=net0",
            "-kernel", str(UBOOT),
            "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
            "-device", "bochs-display",
            "-device", "virtio-blk-device,drive=bootdisk",
            "-device", "virtio-keyboard-device",
            "-device", "virtio-tablet-device",
            "-no-reboot",
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
    repack_boot_disk()

    boot = Boot()
    print("[boot] waiting for U-Boot prompt", flush=True)
    boot.read_until(b"=> ", 60)

    # --- Dynamic PCI BAR probing ---
    FALLBACK_FB_ADDR = 0x4000_0000
    fb_addr = FALLBACK_FB_ADDR

    print("[uboot] pci-enum", flush=True)
    boot.send("pci enum")
    boot.read_until(b"=> ", 30)

    # bochs-display is the first -device on the QEMU cmdline, so it sits at
    # bus 0 device 1 (host bridge is always 0.0.0).
    print("[uboot] pci-bar-probe", flush=True)
    boot.send("pci bar 0.1.0")
    boot.read_until(b"=> ", 30)
    bar_text = bytes(boot.transcript[-3000:]).decode("utf-8", "replace")
    # U-Boot pci bar output format:
    #   ID   Base                Size                Width  Type
    #   ----------------------------------------------------------
    #    0   0x0000000040000000  0x0000000001000000  32     MEM   Prefetchable
    bar_match = _re.search(r"^\s*0\s+(0x[0-9a-fA-F]+)\s+", bar_text, _re.MULTILINE)
    if bar_match:
        fb_addr = int(bar_match.group(1), 16)
        print(f"[ok] bochs-display BAR0 = {fb_addr:#x}", flush=True)
    else:
        print(f"[warn] could not parse BAR0 from U-Boot; falling back to {FALLBACK_FB_ADDR:#x}", flush=True)

    for name, text, expected in _mk_uboot_commands(fb_addr):
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
