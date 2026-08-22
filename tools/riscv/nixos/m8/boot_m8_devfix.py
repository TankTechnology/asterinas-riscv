#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the devtmpfs auto-create regression test on Asterinas RISC-V.

Boots the current kernel with an initramfs that contains **no `/dev`**
directory. Before the fix, the kernel panicked in
``device::init_in_first_process`` ("path resolution did not reach the final
target"); after the fix it must create `/dev`, mount devtmpfs, register
`/dev/console`, and run `/init` to completion.

Exit 0 iff all of these appear: init marker, ``__M8_DEV__=DIR``,
``__M8_CONSOLE__=PRESENT``, ``__M8_OPEN_CONSOLE__=OK``,
``__M8_LOOP_CONTROL__=PRESENT``, init-done marker — and no panic marker.
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

REPO = Path(__file__).resolve().parent.parent.parent.parent.parent
OUT = REPO / "target/nixos/m8/devtmpfs-nodev"
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = OUT / "boot.ext4"

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

PANIC_MARKERS = [
    b"kernel panic", b"Kernel panic", b"Uncaught panic", b"page fault handler failed",
    b"Oops", b"BUG:", b"panic!",
    b"path resolution did not reach the final target",
]
DONE_MARKER = b">>> M8 nodev init done <<<"


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

    def read_until_any(self, needles, timeout):
        deadline = time.monotonic() + timeout
        while not any(needle in self.pending for needle in needles):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timeout waiting {needles!r}; tail={bytes(self.transcript[-600:])!r}")
            self._drain(min(1.0, deadline - time.monotonic()))
        needle = next(needle for needle in needles if needle in self.pending)
        idx = self.pending.index(needle)
        end = idx + len(needle)
        consumed = bytes(self.pending[:end])
        del self.pending[:end]
        return needle, consumed

    def read_until(self, needle, timeout):
        _, consumed = self.read_until_any([needle], timeout)
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
        "-m", "2G", "-smp", "4", "-display", "none", "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-serial", "stdio",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path, default=OUT / "m8-devfix-serial.log")
    args = parser.parse_args()

    if not BOOT_DISK.exists():
        raise SystemExit("missing boot.ext4 — run build_m8_devfix.sh first")
    if not UBOOT.exists():
        raise SystemExit("missing U-Boot — run tools/riscv/prepare_qemu_uboot_booti.sh first")

    boot = Boot(make_argv(), args.serial_log)
    transcript = b""
    reached = "timeout"
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
        marker, _ = boot.read_until_any([DONE_MARKER, *PANIC_MARKERS], 120)
        reached = "init-done" if marker == DONE_MARKER else "panic"
        transcript = bytes(boot.transcript)
    finally:
        boot.close()

    if not transcript:
        transcript = bytes(boot.transcript)

    results = {
        "init-ran": b">>> M8 nodev init:" in transcript,
        "dev-is-dir": b"__M8_DEV__=DIR" in transcript,
        "console-present": b"__M8_CONSOLE__=PRESENT" in transcript,
        "console-open": b"__M8_OPEN_CONSOLE__=OK" in transcript,
        "loop-control-present": b"__M8_LOOP_CONTROL__=PRESENT" in transcript,
        "init-done": DONE_MARKER in transcript,
    }
    done_index = transcript.find(DONE_MARKER)
    pre_done = transcript if done_index < 0 else transcript[:done_index]
    panics = [m.decode() for m in PANIC_MARKERS if m in pre_done]

    print("\n=== devtmpfs auto-create result ===", flush=True)
    for k, v in results.items():
        print(f"  {k}: {'OK' if v else 'FAIL'}", flush=True)
    print(f"  collection-ended: {reached}", flush=True)
    if panics:
        print(f"  panic markers: {panics}", flush=True)

    ok = all(results.values()) and not panics
    print(f"\n=== devtmpfs auto-create: {'PASS' if ok else 'FAIL'} ===", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
