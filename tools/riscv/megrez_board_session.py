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
        --expected-crc32 booti=8hex,dtb=8hex,initrd=8hex \
        [--yes] [--log FILE]

With --mock-qemu, the session talks to a QEMU serial (for harness
testing of the serial/echo/milestone layers; no commands are sent), CRCs
are optional, and --mock-timeout sets the finite milestone deadline.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import select
import sys
import termios
import time

BAUD = 115200
TX_DELAY = 0.02
PROMPT = "=> "
MILESTONES = {
    "kernel_enter": "Enter riscv_boot",
    "banner": "Presented by the Asterinas developers",
    "userspace": "Hello from RISC-V userspace",
}
MILESTONE_SEQUENCE = tuple(MILESTONES)
GATE_PATTERN = re.compile(r"U-Boot (\S+)")
LOAD_RESULT_PATTERN = re.compile(r"(?im)^\s*(\d+)\s+bytes read\b")
CRC_RESULT_PATTERN = re.compile(
    r"(?im)^\s*CRC32 for\s+(0x)?([0-9a-f]+)\b[^\r\n]*==>\s*([0-9a-f]{8})\s*$"
)
UBOOT_ERROR_PATTERN = re.compile(
    r"(?im)^\s*(?:\*\*|error\b|failed\b|command failed\b|"
    r"unknown command\b|bad\b|cannot\b)"
)
FDT_ERROR_PATTERN = re.compile(
    r"(?i)(?:\blibfdt\b|\bFDT_ERR_[A-Z0-9_]+\b|"
    r"\bfdt\b[^\r\n]*\b(?:error|failed)\b)"
)
ARTIFACT_NAMES = frozenset(("booti", "dtb", "initrd"))
MILESTONE_TAIL_LENGTH = max(len(marker) for marker in MILESTONES.values()) - 1
ARTIFACT_NAME_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*"
)
BOOTARGS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._=/,:@+%~-]*")
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
    deadline = time.monotonic() + timeout
    data = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], min(0.1, remaining))
        if r:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                break
            data += chunk
            continue
        if data:
            break
    return data.decode(errors="replace")


def observe_milestones(
    next_index: int, tail: str, text: str
) -> tuple[list[str], int, str]:
    """Advance a strict milestone sequence and retain a split-marker tail."""
    window = tail + text
    found: list[str] = []
    cursor = 0
    while next_index < len(MILESTONE_SEQUENCE):
        occurrences = [
            (position, name)
            for name, marker in MILESTONES.items()
            if (position := window.find(marker, cursor)) >= 0
        ]
        if not occurrences:
            break
        position, name = min(occurrences)
        expected = MILESTONE_SEQUENCE[next_index]
        if name != expected:
            raise RuntimeError(
                f"milestone out of order: expected {expected}, observed {name}"
            )
        found.append(name)
        next_index += 1
        cursor = position + len(MILESTONES[name])
    tail = window[cursor:][-MILESTONE_TAIL_LENGTH:]
    return found, next_index, tail


class BoardSession:
    def __init__(self, device: str, log_path: str, confirm: bool = True):
        self.fd = open_serial(device)
        self.log = open(log_path, "a", buffering=1)
        self.confirm = confirm
        self.milestones: dict[str, float] = {}
        self._milestone_tail = ""
        self._next_milestone = 0

    def _log(self, text: str) -> None:
        self.log.write(text)
        sys.stdout.write(text)

    def wait_for(self, pattern: str, timeout: float) -> str:
        buf = ""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = read_available(self.fd, min(1.0, remaining))
            if not chunk:
                continue
            self._log(chunk)
            buf += chunk
            if pattern in buf:
                return buf
        raise TimeoutError(f"timed out waiting for {pattern!r}; last: {buf[-200:]!r}")

    def send(self, command: str) -> None:
        for i, byte in enumerate(command.encode()):
            os.write(self.fd, bytes((byte,)))
            time.sleep(TX_DELAY)
        os.write(self.fd, b"\r")
        time.sleep(0.2)

    def command(
        self, command: str, expect: str | None = None, timeout: float = 15
    ) -> str:
        if self.confirm and command.startswith(
            ("ext4load", "booti", "setenv bootargs")
        ):
            answer = input(f"send {command!r}? [y/N] ").strip().lower()
            if answer != "y":
                raise RuntimeError("aborted by operator")
        self.send(command)
        out = self.wait_for(expect if expect else PROMPT, timeout)
        normalized_lines = [line.strip() for line in out.replace("\r", "").splitlines()]
        if (
            command not in normalized_lines
            and f"{PROMPT.strip()} {command}" not in normalized_lines
        ):
            raise RuntimeError(f"echo mismatch for {command!r}")
        if UBOOT_ERROR_PATTERN.search(out):
            raise RuntimeError(
                f"U-Boot error while running {command!r}: {out[-200:]!r}"
            )
        if command.startswith("fdt ") and FDT_ERROR_PATTERN.search(out):
            raise RuntimeError(f"FDT error while running {command!r}: {out[-200:]!r}")
        return out

    def load_artifact(
        self, name: str, filename: str, address: int, expected_crc32: str
    ) -> int:
        """Load one artifact and verify U-Boot's size and CRC32 evidence."""
        load_command = f"ext4load mmc 1:1 0x{address:x} /{filename}"
        load_output = self.command(load_command)
        load_result = LOAD_RESULT_PATTERN.search(load_output)
        if load_result is None or int(load_result.group(1)) <= 0:
            raise RuntimeError(f"{name}: no positive 'bytes read' result")

        crc_command = f"crc32 0x{address:x} ${{filesize}}"
        crc_output = self.command(crc_command)
        crc_result = CRC_RESULT_PATTERN.search(crc_output)
        if crc_result is None:
            raise RuntimeError(f"{name}: no parseable CRC32 result")
        actual_address = int(crc_result.group(2), 16)
        actual_crc32 = crc_result.group(3).lower()
        if actual_address != address or actual_crc32 != expected_crc32:
            raise RuntimeError(
                f"{name}: CRC32 mismatch at 0x{actual_address:x}: "
                f"expected {expected_crc32}, got {actual_crc32}"
            )
        return int(load_result.group(1))

    def note_milestone(self, text: str) -> None:
        found, next_index, tail = observe_milestones(
            self._next_milestone, self._milestone_tail, text
        )
        self._next_milestone = next_index
        self._milestone_tail = tail
        for name in found:
            self.milestones[name] = time.monotonic()
            self._log(f"\n[MILESTONE] {name}\n")

    def start_boot_attempt(self) -> None:
        """Discard pre-boot observations and start one ordered boot attempt."""
        self.milestones.clear()
        self._milestone_tail = ""
        self._next_milestone = 0


def parse_expected_crc32(spec: str) -> dict[str, str]:
    """Parse the required logical-artifact CRC map."""
    parsed: dict[str, str] = {}
    for entry in spec.split(","):
        if entry.count("=") != 1:
            raise argparse.ArgumentTypeError("CRCs must use name=8-hex-digits")
        name, value = entry.split("=", 1)
        if name not in ARTIFACT_NAMES:
            raise argparse.ArgumentTypeError(f"unknown CRC artifact name: {name!r}")
        if name in parsed:
            raise argparse.ArgumentTypeError(f"duplicate CRC artifact name: {name}")
        if re.fullmatch(r"[0-9a-fA-F]{8}", value) is None:
            raise argparse.ArgumentTypeError(f"invalid CRC32 for {name}: {value!r}")
        parsed[name] = value.lower()
    if parsed.keys() != ARTIFACT_NAMES:
        missing = sorted(ARTIFACT_NAMES - parsed.keys())
        raise argparse.ArgumentTypeError(f"missing CRC32 entries: {', '.join(missing)}")
    return parsed


def positive_finite_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return seconds


def safe_artifact_name(value: str) -> str:
    if ARTIFACT_NAME_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "artifact names may contain only safe path letters, digits, '/', '.', '_', '+', and '-'"
        )
    return value


def safe_bootargs(value: str) -> str:
    if BOOTARGS_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "bootargs contain a control character or U-Boot shell separator"
        )
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        usage=(
            "%(prog)s DEVICE --booti FILE --initrd FILE --dtb FILE "
            "--expected-crc32 booti=8hex,dtb=8hex,initrd=8hex [options]\n"
            "       %(prog)s DEVICE --booti FILE --initrd FILE --dtb FILE "
            "--mock-qemu [--mock-timeout SECONDS] [options]"
        ),
    )
    p.add_argument("device")
    p.add_argument("--booti", required=True, type=safe_artifact_name)
    p.add_argument("--initrd", required=True, type=safe_artifact_name)
    p.add_argument("--dtb", required=True, type=safe_artifact_name)
    p.add_argument("--bootargs", type=safe_bootargs, default=DEFAULT_BOOTARGS)
    p.add_argument(
        "--expected-crc32",
        type=parse_expected_crc32,
        help="booti=8hex,dtb=8hex,initrd=8hex (required outside mock mode)",
    )
    p.add_argument("--yes", action="store_true", help="skip interactive confirmations")
    p.add_argument("--log", default="megrez-session.log")
    p.add_argument("--mock-qemu", action="store_true", help="harness mode: no commands")
    p.add_argument(
        "--mock-timeout",
        type=positive_finite_seconds,
        default=120.0,
        help="mock milestone deadline in seconds (default: 120)",
    )
    args = p.parse_args(argv)
    if not args.mock_qemu and args.expected_crc32 is None:
        p.error("--expected-crc32 is required outside --mock-qemu mode")
    return args


def run_mock_qemu(device: str, timeout: float) -> int:
    """Watch a QEMU Unix serial socket until all milestones or the deadline."""
    import socket

    seen: set[str] = set()
    next_index = 0
    tail = ""
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(device)
        except OSError as error:
            print(f"mock serial connect failed: {error}", file=sys.stderr)
            return 2
        while len(seen) != len(MILESTONES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(0.25, remaining))
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as error:
                print(f"mock serial read failed: {error}", file=sys.stderr)
                break
            if not data:
                break
            text = data.decode(errors="replace")
            try:
                found, next_index, tail = observe_milestones(next_index, tail, text)
            except RuntimeError as error:
                print(str(error), file=sys.stderr)
                return 2
            for name in found:
                seen.add(name)
                print(f"[MILESTONE] {name}")
    if len(seen) == len(MILESTONES):
        return 0
    print(
        f"missing milestones: {', '.join(sorted(MILESTONES.keys() - seen))}",
        file=sys.stderr,
    )
    return 2


def boot_loaded_artifacts(session: BoardSession, args: argparse.Namespace) -> str:
    """Run the exact guarded U-Boot load, patch, and boot transaction."""
    session.load_artifact("booti", args.booti, 0x80200000, args.expected_crc32["booti"])
    session.load_artifact("dtb", args.dtb, 0xF0000000, args.expected_crc32["dtb"])
    session.command("fdt addr 0xf0000000")
    session.command("fdt resize 0x1000")
    session.load_artifact(
        "initrd", args.initrd, 0x83000000, args.expected_crc32["initrd"]
    )
    session.command("setenv initrd_size ${filesize}")
    session.command(f'setenv bootargs "{args.bootargs}"')
    session.command(f'fdt set /chosen bootargs "{args.bootargs}"')
    session.command("fdt set /chosen asterinas,usb-host /soc/usb@50480000")
    session.start_boot_attempt()
    return session.command(
        "booti 0x80200000 0x83000000:${initrd_size} 0xf0000000",
        expect=MILESTONES["kernel_enter"],
        timeout=30,
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.mock_qemu:
        return run_mock_qemu(args.device, args.mock_timeout)

    session = BoardSession(args.device, args.log, confirm=not args.yes)
    try:
        boot = session.wait_for(PROMPT, timeout=60)
        gate = GATE_PATTERN.search(boot)
        print(f"U-Boot: {gate.group(1) if gate else 'unknown'}")

        session.note_milestone(boot_loaded_artifacts(session, args))

        # Watch for remaining milestones for up to 60s.
        end = time.monotonic() + 60
        while time.monotonic() < end and len(session.milestones) != len(MILESTONES):
            text = read_available(session.fd, min(5, end - time.monotonic()))
            if text:
                session._log(text)
                session.note_milestone(text)
        print(json.dumps(session.milestones))
        return 0 if len(session.milestones) == len(MILESTONES) else 2
    finally:
        session.log.close()
        os.close(session.fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
