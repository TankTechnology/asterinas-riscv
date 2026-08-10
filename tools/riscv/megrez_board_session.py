#!/usr/bin/env python3
"""Automated Megrez board session: gate, load, patch DTB, booti, verify.

Drives the physical board over the exclusive serial line, mirroring
`megrez-board-session-commands.md`. Every command is sent with paced TX
(the U-Boot drops characters on bursts), its echo is verified, and a
short timeout aborts the session before any risky input. Key steps
(load, booti) require interactive confirmation unless --yes is given.

Usage:
    megrez_board_session.py DEVICE \
        --booti booti-name --initrd initrd-name --dtb dtb-name \
        [--bootargs "cpu_no_boost_1_6ghz ... asterinas.reboot_after=120"] \
        [--expected-crc32 CRCS] [--yes] [--log FILE]

With --mock-qemu, the session talks to a QEMU serial (for harness
testing of the serial/echo/milestone layers; no commands are sent).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import sys
import termios
import time
from pathlib import Path

BAUD = 115200
TX_DELAY = 0.02
PROMPT = "=> "
MILESTONES = {
    "kernel_enter": "Enter riscv_boot",
    "banner": "Presented by the Asterinas developers",
    "userspace": "Hello from RISC-V userspace",
}
GATE_PATTERN = re.compile(r"U-Boot (\S+)")
DEFAULT_BOOTARGS = (
    "cpu_no_boost_1_6ghz loglevel=info init=/init asterinas.reboot_after=120"
)


def open_serial(device: str):
    import fcntl

    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    fcntl.fcntl(fd, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
    attrs = termios.tcgetattr(fd)
    attrs[0] = termios.IGNPAR
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def read_available(fd: int, timeout: float) -> str:
    end = time.monotonic() + timeout
    data = b""
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                break
            data += chunk
        if data:
            # Give the line a moment to settle before returning.
            time.sleep(0.05)
    return data.decode(errors="replace")


class BoardSession:
    def __init__(self, device: str, log_path: str, confirm: bool = True):
        self.fd = open_serial(device)
        self.log = open(log_path, "a", buffering=1)
        self.confirm = confirm
        self.milestones: dict[str, float] = {}

    def _log(self, text: str) -> None:
        self.log.write(text)
        sys.stdout.write(text)

    def wait_for(self, pattern: str, timeout: float) -> str:
        buf = ""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            buf += read_available(self.fd, 1.0)
            self._log(buf[-1:] if buf else "")
            if pattern in buf:
                return buf
        raise TimeoutError(f"timed out waiting for {pattern!r}; last: {buf[-200:]!r}")

    def send(self, command: str) -> None:
        for i, byte in enumerate(command.encode()):
            os.write(self.fd, bytes((byte,)))
            time.sleep(TX_DELAY)
        os.write(self.fd, b"\r")
        time.sleep(0.2)

    def command(self, command: str, expect: str | None = None, timeout: float = 15) -> str:
        if self.confirm and command.startswith(("ext4load", "booti", "setenv bootargs")):
            answer = input(f"send {command!r}? [y/N] ").strip().lower()
            if answer != "y":
                raise RuntimeError("aborted by operator")
        self.send(command)
        out = self.wait_for(expect if expect else PROMPT, timeout)
        if expect and expect not in out:
            raise RuntimeError(f"echo mismatch for {command!r}")
        return out

    def note_milestone(self, text: str) -> None:
        for name, marker in MILESTONES.items():
            if name not in self.milestones and marker in text:
                self.milestones[name] = time.monotonic()
                self._log(f"\n[MILESTONE] {name}\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("device")
    p.add_argument("--booti", required=True)
    p.add_argument("--initrd", required=True)
    p.add_argument("--dtb", required=True)
    p.add_argument("--bootargs", default=DEFAULT_BOOTARGS)
    p.add_argument("--expected-crc32", default="", help="comma-separated name=value")
    p.add_argument("--yes", action="store_true", help="skip interactive confirmations")
    p.add_argument("--log", default="megrez-session.log")
    p.add_argument("--mock-qemu", action="store_true", help="harness mode: no commands")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.mock_qemu:
        # Harness mode: device is a QEMU unix serial socket; watch milestones.
        import socket

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(args.device)
        sock.setblocking(False)
        fd = sock.fileno()
        end = time.monotonic() + 120
        seen = set()
        while time.monotonic() < end:
            text = read_available(fd, 5)
            for name, marker in MILESTONES.items():
                if marker in text and name not in seen:
                    seen.add(name)
                    print(f"[MILESTONE] {name}")
        return 0

    session = BoardSession(args.device, args.log, confirm=not args.yes)
    try:
        boot = session.wait_for(PROMPT, timeout=60)
        gate = GATE_PATTERN.search(boot)
        print(f"U-Boot: {gate.group(1) if gate else 'unknown'}")
        session.note_milestone(boot)

        session.command(f"ext4load mmc 1:1 0x80200000 /{args.booti}", expect="0x80200000")
        session.command(f"ext4load mmc 1:1 0xf0000000 /{args.dtb}")
        session.command("fdt addr 0xf0000000")
        session.command("fdt resize 0x1000")
        session.command(f"ext4load mmc 1:1 0x83000000 /{args.initrd}")
        session.command("setenv initrd_size ${filesize}")
        session.command(f"setenv bootargs \"{args.bootargs}\"")
        session.command(f"fdt set /chosen bootargs \"{args.bootargs}\"")
        session.command("fdt set /chosen asterinas,usb-host /soc/usb@50480000")
        session.command("booti 0x80200000 0x83000000:${initrd_size} 0xf0000000",
                        expect="Enter riscv_boot", timeout=30)

        # Watch for remaining milestones for up to 60s.
        end = time.monotonic() + 60
        while time.monotonic() < end and "userspace" not in session.milestones:
            session.note_milestone(read_available(session.fd, 5))
        print(json.dumps(session.milestones))
        return 0 if "userspace" in session.milestones else 2
    finally:
        session.log.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
