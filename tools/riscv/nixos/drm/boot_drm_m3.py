#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the DRM-M3 smoke test and verify Xorg renders via modesetting.

Boots Asterinas RISC-V with a ``virtio-gpu-device`` plus virtio keyboard/tablet.
The guest ``/init`` launches Xorg with the standard **modesetting** driver on
``/dev/dri/card0``, then a small static client fills the root window with a
horizontal green->blue gradient. The host takes a QEMU ``screendump`` and checks
that the gradient actually reached the display — proving the KMS path (dumb
buffers, mmap, SETCRTC, DIRTYFB) drives real desktop output, not just a
boot-time test pattern.

Usage:
    python3 tools/riscv/nixos/drm/boot_drm_m3.py \
        [--u-boot /tmp/drm-m3/u-boot] \
        [--boot-disk /tmp/drm-m3/boot.ext4] \
        [--screenshot /tmp/drm-m3/]
"""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

DONE_MARKER = b"__DRM_XCLIENT_OK__"
BOOT_MARKER = b">>> DRM-M3"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

MON_SOCK = Path("/tmp/drm-m3/mon.sock")


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


def screendump(sock_path: Path, path: Path) -> None:
    for _ in range(20):
        if sock_path.exists():
            break
        time.sleep(0.25)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        s.settimeout(5)
        s.sendall(f"screendump {path}\n".encode())
        time.sleep(2.0)


def read_ppm(path: Path) -> tuple[int, int, list[int]]:
    data = path.read_bytes()
    assert data[:2] == b"P6", f"not a binary PPM: {data[:8]!r}"
    parts = data.split(maxsplit=3)
    assert len(parts) == 4, "unexpected PPM header"
    header = parts[1] + b" " + parts[2] + b" " + parts[3]
    width, height, maxval = header.split()[:3]
    width, height, maxval = int(width), int(height), int(maxval)
    body = parts[3][parts[3].index(b"\n") + 1:]
    return width, height, list(body)


def pixel_at(pixels: list[int], width: int, x: int, y: int) -> tuple[int, int, int]:
    i = (y * width + x) * 3
    return pixels[i], pixels[i + 1], pixels[i + 2]


def verify_pattern(ppm: Path) -> dict:
    """Check the screendump is left-red / right-blue, not black or the boot test
    pattern. The xfill client paints a solid blue root with a red left half."""
    width, height, pixels = read_ppm(ppm)
    mid_y = height // 2
    left = pixel_at(pixels, width, max(1, width // 20), mid_y)
    right = pixel_at(pixels, width, min(width - 2, width - width // 20), mid_y)
    nonblack = sum(pixels) > 0
    red_left = left[0] > 150 and left[0] > left[1] * 2 and left[0] > left[2] * 2
    blue_right = right[2] > 150 and right[2] > right[0] * 2 and right[2] > right[1] * 2
    ok = nonblack and red_left and blue_right
    return {"width": width, "height": height, "left": left, "right": right, "ok": ok}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--u-boot", type=Path, default=Path("/tmp/drm-m3/u-boot"))
    parser.add_argument("--boot-disk", type=Path, default=Path("/tmp/drm-m3/boot.ext4"))
    parser.add_argument("--serial-log", type=Path, default=Path("/tmp/drm-m3/serial.log"))
    parser.add_argument("--shot-dir", type=Path, default=Path("/tmp/drm-m3"))
    parser.add_argument("--command-timeout", type=float, default=300.0)
    parser.add_argument("--smp", type=int, default=1)
    args = parser.parse_args()

    if not args.u_boot.exists():
        raise SystemExit(f"missing U-Boot: {args.u_boot}")
    if not args.boot_disk.exists():
        raise SystemExit(f"missing boot disk: {args.boot_disk}")

    args.shot_dir.mkdir(parents=True, exist_ok=True)
    shot = args.shot_dir / "desktop.ppm"

    if MON_SOCK.exists():
        MON_SOCK.unlink()

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", "none",
        "-monitor", f"unix:{MON_SOCK},server,nowait",
        "-serial", "stdio",
        "-no-reboot",
        "-device", "virtio-gpu-device",
        "-device", "virtio-keyboard-device",
        "-device", "virtio-tablet-device",
        "-d", "guest_errors",
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

        print("[boot] waiting for Xorg (modesetting) to render", flush=True)
        try:
            boot.read_until(DONE_MARKER, args.command_timeout)
        except TimeoutError:
            print("[drm] FAIL: no Xorg render marker (hang/crash)", flush=True)
            tail = bytes(boot.transcript[-6000:])
            print(tail.decode("utf-8", "replace")[-4000:], flush=True)
            return 1

        # Let the server's block handler push the damage to the scanout.
        time.sleep(3)
        screendump(MON_SOCK, shot)
        if shot.exists():
            grad = verify_pattern(shot)
            print(f"[drm] desktop pattern: left={grad.get('left')} "
                  f"right={grad.get('right')} ok={grad['ok']}", flush=True)
        else:
            grad = {"ok": False}
            print("[drm] desktop screenshot missing", flush=True)
    finally:
        boot.close()

    text = bytes(boot.transcript).decode("utf-8", "replace")
    rendered = bool(grad.get("ok"))

    print("\n=== DRM-M3 guest result ===", flush=True)
    for line in text.splitlines():
        if "modeset" in line.lower() or line.startswith("__DRM") or "Xorg" in line:
            print(line, flush=True)
    print(f"  desktop pattern: {'OK' if rendered else 'FAIL'}", flush=True)

    result = rendered
    print(f"\n=== DRM-M3: {'PASS' if result else 'FAIL'} (smp={args.smp}) ===", flush=True)
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
