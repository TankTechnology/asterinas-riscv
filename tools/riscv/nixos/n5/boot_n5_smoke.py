#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""NIXOS-N5 guest gate: stage-1 initramfs -> persistent ext2 root -> systemd.

Boots QEMU twice from the same private disks (/tmp/n5): the first boot
installs the busybox profile onto the persistent root, the second must find
it already there (R1-B). Both boots must reach systemd as PID 1 (stage-2).

Usage:
    python3 tools/riscv/nixos/n5/boot_n5_smoke.py \
        [--serial-log /tmp/n5/serial.log] [--collect-timeout 300]
"""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
DISK_DIR = Path(os.environ.get("N5_DISK_DIR", "/tmp/n5"))
BOOT_DISK = DISK_DIR / "boot.ext4"
ROOT_DISK = DISK_DIR / "root.ext2"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

UBOOT_COMMANDS = [
    ("virtio-scan", "virtio scan", "=>"),
    ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
    ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
    ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
    ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
    ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio.gz", "bytes read"),
    ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
    ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
     "Starting kernel ..."),
]

SYSTEMD_BANNER = b"running in system mode"
BASIC_TARGET = b"Reached target Basic System"


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


def qemu_argv() -> list[str]:
    return [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "1",
        "-display", "none",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-drive", f"if=none,format=raw,file={ROOT_DISK},id=rootdisk",
        "-device", "virtio-blk-device,drive=rootdisk",
        "-netdev", "user,id=n0",
        "-device", "virtio-net-device,netdev=n0",
    ]


def run_boot(boot_no: int, serial_log: Path, collect_timeout: float) -> bytes:
    boot = Boot(qemu_argv(), serial_log)
    try:
        print(f"[boot{boot_no}] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)
        for name, text, expected in UBOOT_COMMANDS:
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        print(f"[boot{boot_no}] kernel started; collecting stage1/stage2 output", flush=True)
        deadline = time.monotonic() + collect_timeout
        while time.monotonic() < deadline:
            if BASIC_TARGET in boot.pending or b"startup finished" in boot.pending.lower():
                break
            try:
                boot._drain(2.0)
            except RuntimeError:
                break
        # Give systemd a few more seconds to settle after the milestone.
        boot._drain(5.0)
        return bytes(boot.transcript)
    finally:
        boot.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path, default=Path("/tmp/n5/serial.log"))
    parser.add_argument("--collect-timeout", type=float, default=300.0)
    args = parser.parse_args()

    for p in (UBOOT, BOOT_DISK, ROOT_DISK):
        if not p.exists():
            raise SystemExit(f"missing {p}; run build_n5.sh first")

    boot1 = run_boot(1, Path(f"{args.serial_log}.boot1"), args.collect_timeout)
    boot2 = run_boot(2, Path(f"{args.serial_log}.boot2"), args.collect_timeout)

    checks = [
        ("boot1 stage1 root switch", b"__N5_STAGE1_OK__" in boot1),
        ("boot1 first-boot install", b"__N5_FIRST_BOOT__" in boot1
         and b"__N5_INSTALL_RC__=0" in boot1),
        ("boot1 systemd stage2", SYSTEMD_BANNER in boot1),
        ("boot2 stage1 root switch", b"__N5_STAGE1_OK__" in boot2),
        ("boot2 profile persisted", b"__N5_PROFILE_PERSISTED__" in boot2),
        ("boot2 profile binary runs", b"__N5_PROFILE_RUNS__" in boot2),
        ("boot2 systemd stage2", SYSTEMD_BANNER in boot2),
    ]

    print("\n=== N5 results ===", flush=True)
    for name, ok in checks:
        print(f"  {name}: {'OK' if ok else 'MISSING'}", flush=True)

    if not all(ok for _, ok in checks):
        print("=== boot1 tail ===", flush=True)
        print(boot1.decode("utf-8", "replace")[-2500:], flush=True)
        print("=== boot2 tail ===", flush=True)
        print(boot2.decode("utf-8", "replace")[-2500:], flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
