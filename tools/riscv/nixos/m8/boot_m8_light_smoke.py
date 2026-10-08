#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the M8 lightweight (busybox-init + nix profile) system on Asterinas RISC-V.

Same QEMU virt / U-Boot booti handoff as boot_m7_daemon_smoke.py. The static
/init (see init_m8_light.c) starts nix-daemon, `nix profile install`s hello
into /nix/var/nix/profiles/default, then runs a login shell (/bin/sh -l) that
sources /etc/profile and executes the installed binary by bare name.
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
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

INIT_MARKER = b">>> M8 init: lightweight nix-profile system <<<"
DONE_MARKER = b">>> M8 lightweight system done <<<"

CHECKS = [
    ("nix profile install exit 0", b"__M8_INSTALL_EXIT__=0"),
    ("profile bin/hello present", b"__M8_PROFILE_BIN__=OK"),
    ("profile sourced (login shell)", b"__M8_PROFILE_SOURCED__"),
    ("login shell PATH has profile", b"/nix/var/nix/profiles/default/bin"),
    ("hello resolved from profile", b"__M8_HELLO__=Hello, world!"),
]

UBOOT_COMMANDS = [
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
                    f"timed out waiting for {needle!r}; tail={self.transcript[-600:]!r}"
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path,
                        default=Path("/tmp/asterinas-m8-light-serial.log"))
    parser.add_argument("--command-timeout", type=float, default=300.0)
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}\nrun prepare_qemu_uboot_booti.sh first")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}\nrun prepare_qemu_uboot_booti.sh first")

    argv = [
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
    ]

    boot = Boot(argv, args.serial_log)
    results = []
    final_tail = b""
    try:
        print("[boot] waiting for U-Boot prompt", flush=True)
        boot.read_until(b"=> ", 60)

        for name, text, expected in UBOOT_COMMANDS:
            print(f"[uboot] {name}", flush=True)
            boot.send(text)
            if name == "booti":
                boot.read_until(b"Starting kernel ...", 90)
            else:
                boot.read_until(expected.encode(), 30)
                if expected != "=>":
                    boot.read_until(b"=> ", 30)

        print("[boot] waiting for init", flush=True)
        boot.read_until(INIT_MARKER, 120)
        print("[ok] init reached", flush=True)

        try:
            boot.read_until(DONE_MARKER, args.command_timeout)
        except TimeoutError:
            final_tail = bytes(boot.transcript[-3000:])
            print("[smoke] did not reach done marker (crash/hang)", flush=True)
            results.append({"command": "done", "status": "CRASH/HANG"})
        else:
            transcript = bytes(boot.transcript)
            for name, needle in CHECKS:
                ok = needle in transcript
                results.append({"command": name, "status": "OK" if ok else "MISSING"})
                print(f"[smoke] {name}: {'OK' if ok else 'MISSING'}", flush=True)

        time.sleep(1)
        final_tail = bytes(boot.transcript[-4000:])
    finally:
        boot.close()

    print("\n=== M8 lightweight smoke results ===", flush=True)
    for r in results:
        print(f"  {r['command']}: {r['status']}", flush=True)
    if final_tail:
        print("\n--- serial tail ---", flush=True)
        print(final_tail.decode(errors="replace")[-2500:], flush=True)

    return 0 if results and all(r["status"] == "OK" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
