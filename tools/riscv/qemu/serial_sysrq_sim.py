#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Status: manual -- serial SysRq QEMU checks; run with `rt sysrq-sim`.
"""Serial SysRq on a kernel whose userspace can no longer run anything.

The 2026-11-11 board lost its console shell and every software recovery net
was already disarmed; only hands on the board could bring it back. This suite
drives the one input path that needs no process -- a UART break followed by a
key -- and checks that it restarts the kernel, that it stays quiet otherwise,
and that it cannot be triggered by ordinary console bytes.

QEMU's telnet chardev forwards IAC BRK (0xff 0xf3) as a real break condition,
which is what the 16550 model turns into LSR.BI.
"""

from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from tools.riscv.qemu.recovery_sim import (  # noqa: E402
    MAX_RELEASE_IMAGE_BYTES,
    build_pause_initramfs,
)

DEFAULT_IMAGE = REPO / "target/osdk/aster-kernel-osdk-bin.Image"
IAC_BRK = b"\xff\xf3"
BOOT_MARKER = "OpenSBI v"
# A loaded host can take a long time to reach the banner; the checks below only
# need the UART component to be initialized, which happens early.
BOOT_TIMEOUT = 60
SYSRQ_BREAK = "ASTERINAS_SERIAL_SYSRQ break"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TelnetConsole:
    def __init__(self, port: int, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)
        self.sock.settimeout(0.2)
        self.text = ""

    def pump(self, seconds: float) -> str:
        end = time.time() + seconds
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self.text += chunk.decode("utf-8", "replace")
        return self.text

    def wait_for(self, needle: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.2)
            if needle in self.text:
                return True
        return False

    def count(self, needle: str) -> int:
        return self.text.count(needle)

    def break_then(self, key: bytes) -> None:
        self.sock.sendall(IAC_BRK)
        time.sleep(0.1)
        self.sock.sendall(key)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def start(
    qemu: str, image: Path, initrd: Path, append: str
) -> tuple[subprocess.Popen, TelnetConsole, Path]:
    port = free_port()
    error_path = Path(f"/tmp/qemu-sysrq-{port}.err")
    error_file = open(error_path, "wb")
    process = subprocess.Popen(
        [
            qemu,
            "-machine",
            "virt",
            "-m",
            "1G",
            "-smp",
            "2",
            "-display",
            "none",
            # Start paused so the telnet client is connected before the guest
            # prints its first byte; otherwise a fast banner can be lost.
            "-S",
            "-monitor",
            f"unix:/tmp/qemu-sysrq-{port}.mon,server=on,wait=off",
            "-serial",
            f"telnet:127.0.0.1:{port},server=on,wait=off",
            "-kernel",
            str(image),
            "-initrd",
            str(initrd),
            "-append",
            append,
        ],
        stdout=subprocess.DEVNULL,
        stderr=error_file,
    )
    # Keep the file object alive for the child; it is closed by `stop`.
    _STDERR_FILES[id(process)] = error_file
    console = TelnetConsole(port)
    _resume(f"/tmp/qemu-sysrq-{port}.mon")
    return process, console, error_path


_STDERR_FILES: dict[int, object] = {}


def _resume(monitor: str) -> None:
    """Releases the paused CPU once the serial client is attached."""
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.socket(socket.AF_UNIX) as monitor_socket:
                monitor_socket.connect(monitor)
                monitor_socket.sendall(b"cont\n")
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("QEMU monitor did not accept the resume command")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    handle = _STDERR_FILES.pop(id(process), None)
    if handle is not None:
        handle.close()


def qemu_failure(process: subprocess.Popen, error_path: Path) -> str:
    """A one-line reason a case could not even boot."""
    try:
        detail = error_path.read_text(errors="replace").strip().splitlines()
    except OSError:
        detail = []
    tail = detail[-1] if detail else ""
    return f"qemu rc={process.poll()} stderr={tail[:160]}"


def wait_for_count(console: TelnetConsole, needle: str, minimum: int, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        console.pump(0.2)
        if console.count(needle) >= minimum:
            return True
    return False


def boot(process: subprocess.Popen, console: TelnetConsole, error_path: Path) -> str | None:
    if not console.wait_for(BOOT_MARKER, BOOT_TIMEOUT):
        return "no boot marker; " + qemu_failure(process, error_path)
    # The UART component is registered during component initialization, before
    # the init process is spawned. Waiting for that marker means a break sent
    # now will reach the driver instead of being lost.
    if not console.wait_for("ASTERINAS_BOOT_ID", 30):
        console.pump(4.0)
    else:
        console.pump(1.0)
    return None


def case_break_reboots(qemu: str, image: Path, initrd: Path, append: str) -> tuple[bool, str]:
    process, console, error_path = start(qemu, image, initrd, append)
    try:
        if (failure := boot(process, console, error_path)) is not None:
            return False, failure
        boots = console.count(BOOT_MARKER)
        console.break_then(b"b")
        if not console.wait_for("ASTERINAS_SERIAL_SYSRQ command=b", 10):
            return False, "the kernel did not see break+b"
        if not wait_for_count(console, BOOT_MARKER, boots + 1, 30):
            return False, "no second boot; " + qemu_failure(process, error_path)
        return True, f"boots={console.count(BOOT_MARKER)}"
    finally:
        console.close()
        stop(process)


def case_break_powers_off(qemu: str, image: Path, initrd: Path, append: str) -> tuple[bool, str]:
    process, console, error_path = start(qemu, image, initrd, append)
    try:
        if (failure := boot(process, console, error_path)) is not None:
            return False, failure
        console.break_then(b"o")
        end = time.time() + 20
        while time.time() < end:
            console.pump(0.5)
            if process.poll() is not None:
                return True, "QEMU exited"
        if "ASTERINAS_SERIAL_SYSRQ command=o" not in console.text:
            return False, "the kernel did not see break+o"
        return False, "QEMU kept running after break+o"
    finally:
        console.close()
        stop(process)


def case_break_timeout_does_nothing(
    qemu: str, image: Path, initrd: Path, append: str
) -> tuple[bool, str]:
    process, console, error_path = start(qemu, image, initrd, append)
    try:
        if (failure := boot(process, console, error_path)) is not None:
            return False, failure
        boots = console.count(BOOT_MARKER)
        console.sock.sendall(IAC_BRK)
        if not console.wait_for(SYSRQ_BREAK, 10):
            return False, "the kernel did not see the break"
        # The window is 2 s; this key must be ordinary input.
        time.sleep(3.0)
        console.sock.sendall(b"b")
        console.pump(6.0)
        if "command=b" in console.text:
            return False, "a stale break rebooted the kernel"
        if console.count(BOOT_MARKER) != boots:
            return False, "boot count changed"
        return True, "window expired"
    finally:
        console.close()
        stop(process)


def case_plain_key_does_nothing(
    qemu: str, image: Path, initrd: Path, append: str
) -> tuple[bool, str]:
    process, console, error_path = start(qemu, image, initrd, append)
    try:
        if (failure := boot(process, console, error_path)) is not None:
            return False, failure
        boots = console.count(BOOT_MARKER)
        console.sock.sendall(b"b")
        console.pump(6.0)
        if SYSRQ_BREAK in console.text:
            return False, "a plain byte looked like a break"
        if console.count(BOOT_MARKER) != boots:
            return False, "boot count changed"
        return True, "plain input ignored"
    finally:
        console.close()
        stop(process)


def case_break_with_other_key_does_nothing(
    qemu: str, image: Path, initrd: Path, append: str
) -> tuple[bool, str]:
    process, console, error_path = start(qemu, image, initrd, append)
    try:
        if (failure := boot(process, console, error_path)) is not None:
            return False, failure
        boots = console.count(BOOT_MARKER)
        console.break_then(b"x")
        if not console.wait_for(SYSRQ_BREAK, 10):
            return False, "the kernel did not see the break"
        console.pump(5.0)
        if "command=" in console.text:
            return False, "an unknown key was treated as a command"
        if console.count(BOOT_MARKER) != boots:
            return False, "boot count changed"
        return True, "unknown key delivered as input"
    finally:
        console.close()
        stop(process)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--qemu", default="qemu-system-riscv64")
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args(argv)

    image = args.image
    if not image.is_file():
        print(f"missing kernel image: {image}", file=sys.stderr)
        return 2
    if image.stat().st_size > MAX_RELEASE_IMAGE_BYTES:
        print(
            f"{image} is {image.stat().st_size} bytes; a dev/ktest build "
            "overwrote the release image (rebuild with the release profile)",
            file=sys.stderr,
        )
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="sysrq-sim-"))
    try:
        initrd = build_pause_initramfs(workdir)
        append = "console=ttyS0 loglevel=warn init=/init"
        cases = [
            ("break-b-reboots", case_break_reboots),
            ("break-o-powers-off", case_break_powers_off),
            ("break-timeout-does-nothing", case_break_timeout_does_nothing),
            ("plain-key-does-nothing", case_plain_key_does_nothing),
            ("break-unknown-key-does-nothing", case_break_with_other_key_does_nothing),
        ]
        selected = set(args.only)
        failures = 0
        print(f"{'case':38} result")
        for name, run in cases:
            if selected and name not in selected:
                continue
            try:
                ok, detail = run(args.qemu, image, initrd, append)
            except Exception as error:  # noqa: BLE001 - report, do not abort
                ok, detail = False, f"{type(error).__name__}: {error}"
            failures += 0 if ok else 1
            print(f"{name:38} {'PASS' if ok else 'FAIL'}  {detail}", flush=True)
        print(f"\n{'all sysrq checks passed' if failures == 0 else f'{failures} FAILED'}")
        return 0 if failures == 0 else 1
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
