#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""DRM-M7 persistent-storage smoke test.

Boots the DRM-tree kernel twice with the same ext2 data disk attached as the
second virtio-blk device (/dev/vdb):

  boot 1 — /init mounts /dev/vdb at /home, writes /home/PERSISTED, sync.
  boot 2 — /init mounts the *same* disk, reads /home/PERSISTED back.

Exit 0 iff boot 1 reports ``__M7_PERSIST__=WROTE`` and boot 2 reports
``__M7_PERSIST__=SURVIVED``, proving the file survived the reboot.
"""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

OUT = Path("/tmp/drm-m7")
UBOOT = OUT / "u-boot"
BOOT_DISK = OUT / "boot.ext4"
PERSIST_DISK = OUT / "nix-store.ext2"

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
    ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
    ("initrd-load", f"ext4load virtio 0:0 {INITRD_LOAD:#x} /initramfs.cpio", "bytes read"),
    ("initrd-size-save", "setenv initrd_size ${filesize}", "=>"),
    ("booti", f"booti {KERNEL_LOAD:#x} {INITRD_LOAD:#x}:${{initrd_size}} {DTB_LOAD:#x}",
     "Starting kernel ..."),
]


class Boot:
    def __init__(self, argv, serial_log):
        serial_log.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = serial_log.open("wb")
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.proc.stdout, selectors.EVENT_READ)
        self.pending = bytearray()
        self.transcript = bytearray()

    def _drain(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for key, _ in self.sel.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial closed")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)

    def read_until(self, needle, timeout):
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timeout waiting {needle!r}; tail={bytes(self.transcript[-600:])!r}")
            self._drain(min(1.0, deadline - time.monotonic()))
        idx = self.pending.index(needle)
        end = idx + len(needle)
        consumed = bytes(self.pending[:end])
        del self.pending[:end]
        return consumed

    def send(self, text):
        self.proc.stdin.write((text + "\n").encode())
        self.proc.stdin.flush()

    def close(self):
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


def make_argv() -> list[str]:
    return [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G", "-smp", "1", "-display", "none", "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-drive", f"if=none,format=raw,file={PERSIST_DISK},id=nixdisk",
        "-device", "virtio-blk-device,drive=nixdisk",
        "-serial", "stdio",
    ]


def run_boot(tag: str, serial_log: Path) -> bytes:
    print(f"[{tag}] booting QEMU", flush=True)
    boot = Boot(make_argv(), serial_log)
    try:
        boot.read_until(b"=> ", 60)
        for name, text, expected in UBOOT_COMMANDS:
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)
        boot.read_until(b">>> M7 init done <<<", 120)
        return bytes(boot.transcript)
    finally:
        boot.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path, default=OUT / "m7-serial.log")
    args = parser.parse_args()

    if not BOOT_DISK.exists():
        raise SystemExit("missing boot.ext4 — run build_m7.sh first")
    if not PERSIST_DISK.exists():
        raise SystemExit("missing nix-store.ext2 — run build_m7.sh first")

    results = []

    t1 = run_boot("boot1", args.serial_log)
    results.append(("boot1 wrote sentinel", b"__M7_PERSIST__=WROTE" in t1))
    print(f"[boot1] wrote sentinel: {results[-1][1]}", flush=True)

    t2 = run_boot("boot2", args.serial_log)
    results.append(("boot2 sentinel survived", b"__M7_PERSIST__=SURVIVED" in t2))
    print(f"[boot2] sentinel survived: {results[-1][1]}", flush=True)

    print("\n=== DRM-M7 persistence smoke results ===", flush=True)
    for name, ok in results:
        print(f"  {name}: {'OK' if ok else 'FAIL'}", flush=True)

    ok = all(v for _, v in results)
    print(f"\n=== DRM-M7 persistence: {'PASS' if ok else 'FAIL'} ===", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
