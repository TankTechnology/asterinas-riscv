#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Status: prototype -- Renode serial-SysRq test; not part of the default gates.
"""Exercise serial SysRq (break + key) on the Renode board-like platform.

QEMU can deliver a real break over its telnet chardev, but its RISC-V machines
have no DesignWare UART, so that test only covers the ns16550a path. This
script boots the same release kernel used on the board, binds the real
``dw_apb`` driver, and injects a break into Renode's 16550 model in the way the
board's hardware presents one:

* the receiver holds a zero byte (a break is a framing condition on a zero),
* ``LSR.BI`` is set,
* ``IIR`` reports ``ReceiverLineStatusIrq``, which is what the driver's
  ``acknowledge_interrupt_cause`` reads first.

Renode's 16550 model has no ``SendBreak``. The monitor's ``python`` command runs
IronPython on the emulation thread, so it can set the model's ``lineStatus`` /
``interruptIdentification`` fields and force the IRQ line between two guest
instructions. That keeps the injection atomic with respect to the guest.

Three cases run in one boot, in this order so that a wedged guest is not
mistaken for a passed test:

1. a plain key (no break) is delivered to the tty and does not reboot;
2. a break followed by an unbound key is delivered and does not reboot;
3. a break followed by the command key reboots (the guest halts because this
   platform has no reset device and OpenSBI has no reset extension).

Usage:

    tools/riscv/renode/renode_sysrq.py --opensbi /path/to/fw_jump.bin
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from renode_smoke import (  # noqa: E402
    DEFAULT_KERNEL,
    build_fixtures,
    find_renode,
)

DEFAULT_WORKDIR = Path.home() / "renode-megrez-lite-sysrq"
BOOT_MARKER = "ASTERINAS_BOOT_PHASE phase=init-spawned"
BREAK_MARKER = "ASTERINAS_SERIAL_SYSRQ break"
# The platform's syscon peripheral records what OpenSBI's reset driver writes.
# `0xbad` comes from the `syscon-reboot` node, `0xdead` from `syscon-poweroff`.
SYSCON_REBOOT = "SYSCON write 0xbad"
SYSCON_POWEROFF = "SYSCON write 0xdead"

# Runs on Renode's emulation thread through the monitor's `python` command.
# The enum spellings come from the model's own table:
#   LineStatus: DataReady, BreakIrqIndicator, TransmitHoldEmpty, TransmitterEmpty
#   InterruptLevel: ReceiverLineStatusIrq, ReceiverDataIrq, ...
# THRE|TEMT are kept set or the kernel's transmit path would stop polling ready.
BREAK_SCRIPT = """import System
u = self.Machine['sysbus.uart']
t = u.GetType()
bf = System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic
lsf = t.GetField('lineStatus', bf)
u.WriteChar(System.Byte(0))
lsType = lsf.FieldType
lsf.SetValue(u, System.Enum.Parse(lsType, "DataReady, BreakIrqIndicator, TransmitHoldEmpty, TransmitterEmpty"))
iif = t.GetField('interruptIdentification', bf)
{set_cause}
irq = t.GetField('<IRQ>k__BackingField', bf).GetValue(u)
if not irq.IsSet:
    irq.Set(True)
print("BREAK-INJECTED irq=%s cause=%s" % (irq.IsSet, iif.GetValue(u)))
"""

BYTES_SCRIPT = """u = self.Machine['sysbus.uart']
f = open(r'{payload}', 'rb')
d = f.read()
f.close()
for i in range(len(d)):
    b = d[i]
    if isinstance(b, str):
        b = ord(b)
    u.WriteChar(System.Byte(b))
print("BYTES-INJECTED %d" % len(d))
"""


class Monitor:
    def __init__(self, port: int, timeout: float = 30.0):
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.3)
        self.sock.settimeout(0.5)
        self._drain(0.5)

    def _drain(self, wait: float) -> str:
        data = b""
        end = time.time() + wait
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                pass
        return data.decode(errors="replace")

    def run(self, command: str, timeout: float = 5.0) -> str:
        # Read until the monitor prompt, not for a fixed period. Renode's guest
        # clock tracks wall time here, so every millisecond spent draining the
        # reply is a millisecond the SysRq window loses.
        self._drain(0.02)
        self.sock.sendall((command + "\n").encode())
        data = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"(megrez-lite-sysrq)" in data:
                    break
            except socket.timeout:
                pass
        reply = data.decode(errors="replace")
        if "There was an error executing command" in reply:
            raise RuntimeError(f"monitor rejected {command!r}:\n{reply}")
        return reply

    def inject(self, script: Path) -> str:
        # The monitor's `python` does not exclude the emulation thread: without
        # a pause, the guest can service the IRQ that `WriteChar` raises before
        # the line-status poke lands, and the break degrades into a NUL. Pause
        # around the poke so the guest sees one coherent break condition.
        self.run("pause")
        try:
            reply = self.run(f'python "exec(open(r\'{script}\').read())"')
        finally:
            self.run("start")
        return reply.strip()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def wait_for(log: Path, offset: int, marker: str, timeout: float) -> tuple[bool, int]:
    """Wait for `marker` to appear after byte `offset`; return (seen, new offset).

    Poll tightly: anything injected after this wait must still fall inside the
    2 s SysRq window, and the guest clock here runs at about wall speed.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.exists():
            data = log.read_bytes()
            if marker.encode() in data[offset:]:
                return True, len(data)
        time.sleep(0.02)
    return False, offset


def tail_after(log: Path, offset: int, limit: int = 600) -> str:
    if not log.exists():
        return ""
    return log.read_bytes()[offset:].decode(errors="replace")[-limit:]


def write_script(workdir: Path, name: str, text: str) -> Path:
    path = workdir / name
    path.write_text(text, encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    renode, environment = find_renode()
    initramfs = build_fixtures(args.workdir, args.kernel, args.opensbi)
    uart_log = args.workdir / "uart.log"
    if uart_log.exists():
        uart_log.unlink()

    syscon_status = args.workdir / "syscon-status.txt"
    syscon_status.unlink(missing_ok=True)
    payload = args.workdir / "payload.bin"
    set_cause = (
        "iif.SetValue(u, System.Enum.Parse(iif.FieldType, 'ReceiverLineStatusIrq'))"
        if args.cause == "rlsi"
        else "iif.SetValue(u, System.Enum.Parse(iif.FieldType, 'ReceiverDataIrq'))"
    )
    break_script = write_script(
        args.workdir, "inject_break.py", BREAK_SCRIPT.format(set_cause=set_cause)
    )
    bytes_script = write_script(
        args.workdir, "inject_bytes.py", BYTES_SCRIPT.format(payload=payload)
    )

    commands = "; ".join(
        [
            'mach create "megrez-lite-sysrq"',
            f"machine LoadPlatformDescription @{args.workdir}/megrez-lite.repl",
            f"sysbus LoadBinary @{args.opensbi} 0x80000000",
            f"sysbus LoadBinary @{args.kernel} 0x80200000",
            f"sysbus LoadBinary @{args.workdir}/megrez-lite.dtb 0x88000000",
            f"sysbus LoadBinary @{initramfs} 0x88300000",
            f"sysbus LoadBinary @{args.workdir}/bootrom.bin 0x1000",
            "cpu PC 0x1000",
            f"uart CreateFileBackend @{uart_log} true",
            "logLevel 3",
            "start",
        ]
    )
    run_log = args.workdir / "renode.log"
    with open(run_log, "wb") as log:
        process = subprocess.Popen(
            [renode, "--disable-gui", "--plain", "-P", str(args.monitor_port), "-e", commands],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    failures: list[str] = []
    monitor: Monitor | None = None
    try:
        seen, offset = wait_for(uart_log, 0, BOOT_MARKER, args.boot_timeout)
        if not seen:
            print("boot marker NOT reached")
            print(tail_after(uart_log, max(0, offset - 2000)))
            return 1
        print("boot marker reached")
        try:
            monitor = Monitor(args.monitor_port)
        except OSError as exc:
            print(f"monitor connect failed: {exc}")
            return 1

        def send_bytes(data: bytes) -> None:
            payload.write_bytes(data)
            reply = monitor.inject(bytes_script)
            print(f"  inject {data!r}: {reply.splitlines()[-1] if reply else '(no reply)'}")

        def inject_break() -> None:
            reply = monitor.inject(break_script)
            print(f"  inject break: {reply.splitlines()[-1] if reply else '(no reply)'}")

        def case_failed(before: int) -> bool:
            return len(failures) > before

        # Case 1: an ordinary line must reach the tty (echo proves the line
        # discipline got it) and must not be mistaken for a command.
        before = len(failures)
        case_start = len(uart_log.read_bytes()) if uart_log.exists() else 0
        send_bytes(b"plain-key\r")
        seen, _ = wait_for(uart_log, case_start, "plain-key", 5.0)
        if not seen:
            failures.append("case 1: ordinary input was not echoed to the tty")
        time.sleep(0.5)
        if "ASTERINAS_SERIAL_SYSRQ" in tail_after(uart_log, case_start):
            failures.append("case 1: a plain key produced a SysRq action")
        print("case 1 plain key: " + ("FAILED" if case_failed(before) else "ok"))

        # Case 2: a break arms the window, but an unbound key cancels it and
        # is delivered like any other input.
        before = len(failures)
        case_start = len(uart_log.read_bytes())
        inject_break()
        seen, break_offset = wait_for(uart_log, case_start, BREAK_MARKER, 5.0)
        if not seen:
            failures.append("case 2: the break was not reported by the kernel")
            print(tail_after(uart_log, max(0, case_start - 800)))
        send_bytes(b"unbound-key\r")
        seen, _ = wait_for(uart_log, break_offset, "unbound-key", 5.0)
        if not seen:
            failures.append("case 2: the unbound key was not delivered")
        time.sleep(0.5)
        if "command=" in tail_after(uart_log, case_start):
            failures.append("case 2: an unbound key triggered a command")
        print("case 2 break + unbound key: " + ("FAILED" if case_failed(before) else "ok"))

        # Case 3: break + command key must take the action. The platform's
        # syscon records the write OpenSBI's reset driver performs, which is
        # the same contract the board's firmware provides with its own reset
        # device. The guest then stops: OpenSBI sits in its hart-hang loop
        # (the Renode machine reset brings the CPU back to the BootROM, but
        # the generic firmware stalls in HSM init before printing again).
        expected = SYSCON_REBOOT if args.key == "b" else SYSCON_POWEROFF
        before = len(failures)
        syscon_offset = syscon_status.stat().st_size if syscon_status.exists() else 0
        case_start = len(uart_log.read_bytes())
        inject_break()
        seen, marker_offset = wait_for(uart_log, case_start, BREAK_MARKER, 5.0)
        if not seen:
            failures.append("case 3: the break was not reported by the kernel")
        send_bytes(args.key.encode())
        marker = f"ASTERINAS_SERIAL_SYSRQ command={args.key}"
        seen, marker_offset = wait_for(uart_log, marker_offset, marker, 5.0)
        if not seen:
            failures.append(f"case 3: {marker} did not appear")
            print(tail_after(uart_log, max(0, marker_offset - 800)))
        seen, _ = wait_for(syscon_status, syscon_offset, expected, 5.0)
        if not seen:
            failures.append(f"case 3: the platform never saw {expected!r}")
        print(f"case 3 break + {args.key}: " + ("FAILED" if case_failed(before) else "ok"))

        if not case_failed(before):
            # The action must take the guest out of service: a fresh line can
            # no longer echo.
            before = len(failures)
            aftermath_start = len(uart_log.read_bytes())
            send_bytes(b"after-action\r")
            seen, _ = wait_for(uart_log, aftermath_start, "after-action", 2.0)
            if seen:
                failures.append("case 3: the guest still echoed input after the action")
            print("case 3 aftermath: " + ("FAILED" if case_failed(before) else "ok"))
    finally:
        if monitor is not None:
            monitor.close()
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=10)

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nall SysRq cases passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opensbi", type=Path, required=True, help="built fw_jump.bin")
    parser.add_argument("--kernel", type=Path, default=DEFAULT_KERNEL)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--monitor-port", type=int, default=34573)
    parser.add_argument("--boot-timeout", type=float, default=240.0)
    parser.add_argument(
        "--cause",
        choices=("rlsi", "rdi"),
        default="rlsi",
        help="interrupt the injected break reports (board hardware uses rlsi)",
    )
    parser.add_argument("--key", default="b", help="command key for case 3")
    args = parser.parse_args(argv)
    if args.key not in ("b", "o", "h"):
        parser.error("--key must be one of b, o, h")
    args.workdir.mkdir(parents=True, exist_ok=True)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
