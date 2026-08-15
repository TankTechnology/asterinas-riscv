#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the DRM-M4 cursor smoke test and verify the hardware-cursor path.

Boots Asterinas RISC-V with a ``virtio-gpu-device``. The guest ``/init`` drives
``/dev/dri/card0`` through the legacy cursor ioctls (MODE_CURSOR2 to set the
cursor buffer + hotspot, MODE_CURSOR to move it, and MODE_CURSOR with handle 0
to hide it).

The cursor overlay is not composited into QEMU's console surface, so a
``screendump`` cannot see it. Verification is therefore two-pronged:

1. **ioctl acceptance** — every cursor ioctl returns 0 only if QEMU answered
   ``OK_NODATA`` (i.e. it parsed and accepted the cursor command; a malformed
   or invalid command would set ``error_happened`` and the guest would report
   FAIL). The guest prints a marker per step.
2. **device trace** — QEMU's ``virtio_gpu_update_cursor`` trace event fires when
   the device processes an UPDATE_CURSOR/MOVE_CURSOR command, proving the
   command reached the device.

Usage:
    python3 tools/riscv/nixos/drm/boot_drm_m4.py \
        [--u-boot /tmp/drm-m4/u-boot] [--boot-disk /tmp/drm-m4/boot.ext4]
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

DONE_MARKER = b"__DRM_DONE__"
PASS_MARKER = b"__DRM_PASS__"
FAIL_MARKER = b"__DRM_FAIL__"
SET_MARKER = b"__DRM_CURSOR_SET_OK__"
MOVE_MARKER = b"__DRM_CURSOR_MOVE_OK__"
HIDE_MARKER = b"__DRM_CURSOR_HIDE_OK__"

CURSOR_TRACE = b"virtio_gpu_update_cursor"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000


def uboot_commands() -> list[tuple[str, str, str]]:
    return [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=info init=/init"', "=>"),
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

    def read_until(self, needle: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for {needle!r}; tail={self.transcript[-800:]!r}"
                )
            for key, _ in self.sel.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial process closed output")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)
        idx = self.pending.index(needle)
        end = idx + len(needle)
        consumed = bytes(self.pending[:end])
        del self.pending[:end]
        return consumed

    def read_until_any(self, needles: list[bytes], timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            for needle in needles:
                if needle in self.pending:
                    return self.read_until(needle, 1.0)
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out waiting for {needles!r}; tail={self.transcript[-800:]!r}"
                )
            for key, _ in self.sel.select(min(0.1, deadline - time.monotonic())):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("serial process closed output")
                self.transcript.extend(chunk)
                self.log_file.write(chunk)
                self.log_file.flush()
                self.pending.extend(chunk)

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
    parser.add_argument("--u-boot", type=Path, default=Path("/tmp/drm-m4/u-boot"))
    parser.add_argument("--boot-disk", type=Path, default=Path("/tmp/drm-m4/boot.ext4"))
    parser.add_argument("--serial-log", type=Path, default=Path("/tmp/drm-m4/serial.log"))
    parser.add_argument("--command-timeout", type=float, default=180.0)
    parser.add_argument("--smp", type=int, default=1)
    args = parser.parse_args()

    if not args.u_boot.exists():
        raise SystemExit(f"missing U-Boot: {args.u_boot}")
    if not args.boot_disk.exists():
        raise SystemExit(f"missing boot disk: {args.boot_disk}")

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-device", "virtio-gpu-device",
        "-d", "guest_errors",
        "-trace", "virtio_gpu_update_cursor",
        "-kernel", str(args.u_boot),
        "-drive", f"if=none,format=raw,file={args.boot_disk},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
    ]

    boot = Boot(argv, args.serial_log)
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

        print("[boot] waiting for cursor smoke test", flush=True)
        try:
            boot.read_until(DONE_MARKER, args.command_timeout)
            boot.read_until_any([PASS_MARKER, FAIL_MARKER], 60)
        except TimeoutError:
            print("[drm] FAIL: no cursor-test completion (hang/crash)", flush=True)
            tail = bytes(boot.transcript[-4000:])
            print(tail.decode("utf-8", "replace")[-3000:], flush=True)
            return 1
    finally:
        boot.close()

    text = bytes(boot.transcript)
    passed = PASS_MARKER in text and FAIL_MARKER not in text
    set_ok = SET_MARKER in text
    move_ok = MOVE_MARKER in text
    hide_ok = HIDE_MARKER in text
    trace_count = text.count(CURSOR_TRACE)

    print("\n=== DRM-M4 guest result ===", flush=True)
    for line in text.decode("utf-8", "replace").splitlines():
        if line.startswith("[DRM]") or line.startswith("__DRM"):
            print(line, flush=True)
    print(f"  set_cursor2: {'OK' if set_ok else 'FAIL'}", flush=True)
    print(f"  move_cursor: {'OK' if move_ok else 'FAIL'}", flush=True)
    print(f"  hide_cursor: {'OK' if hide_ok else 'FAIL'}", flush=True)
    print(f"  device trace (virtio_gpu_update_cursor): {trace_count} event(s)", flush=True)

    # The device trace is the proof the command reached QEMU; the markers prove
    # QEMU accepted each command (returned OK_NODATA). Require both.
    result = passed and set_ok and move_ok and hide_ok and trace_count >= 1
    print(f"\n=== DRM-M4: {'PASS' if result else 'FAIL'} (smp={args.smp}) ===", flush=True)
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
