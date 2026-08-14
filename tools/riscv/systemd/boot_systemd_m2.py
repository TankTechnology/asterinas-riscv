#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""SYSTEMD-BOOT-M2 driver: boot systemd, log in over getty@ttyS0, and drive a
script of interactive commands to verify getty/login, custom service lifecycle
(simple + forking), socket activation, and journald.

Same QEMU virt / U-Boot ``booti`` handoff as boot_systemd_smoke.py, but instead
of just collecting until basic.target it waits for the busybox getty login
prompt, logs in as root, and runs each probe command, matching a distinctive
marker on success. Exits 0 iff the login shell came up and every probe's marker
was seen; otherwise dumps the transcript tail and exits 1.
"""

from __future__ import annotations

import argparse
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

REPO = Path(__file__).resolve().parent.parent.parent.parent
UBOOT = REPO / "target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = REPO / "target/qemu-uboot/current/boot.ext4"

KERNEL_LOAD = 0x8020_0000
INITRD_LOAD = 0x8300_0000
DTB_LOAD = 0x8800_0000

INIT_MARKER = b">>> systemd init: launching systemd (PID 1) <<<"
LOGIN_PROMPT = b"login:"
SHELL_READY = b"___LOGIN_SHELL_READY___"

# (name, command, expected-marker) probes run in the root login shell.
PROBES = [
    ("shell-echo", "echo M2_SHELL_OK", "M2_SHELL_OK"),
    ("simpletest-start", "systemctl start simpletest && echo M2_SIMPLE_STARTED", "M2_SIMPLE_STARTED"),
    ("simpletest-marker", "cat /run/simpletest.started", "simpletest started pid="),
    ("simpletest-active", "systemctl is-active simpletest && echo M2_SIMPLE_ACTIVE", "M2_SIMPLE_ACTIVE"),
    ("forktest-start", "systemctl start forktest && echo M2_FORK_STARTED", "M2_FORK_STARTED"),
    ("forktest-pid", "test -s /run/forktest.pid && cat /run/forktest.pid && echo M2_FORK_PID", "M2_FORK_PID"),
    ("forktest-active", "systemctl is-active forktest && echo M2_FORK_ACTIVE", "M2_FORK_ACTIVE"),
    ("socktest-socket-start", "systemctl start socktest.socket && echo M2_SOCKET_STARTED", "M2_SOCKET_STARTED"),
    ("socktest-connect", "/usr/bin/sockclient /run/socktest.sock", "hello-from-socket-activated-service"),
    ("journald-start", "systemctl start systemd-journald && echo M2_JOURNALD_STARTED", "M2_JOURNALD_STARTED"),
    ("journald-active", "systemctl is-active systemd-journald && echo M2_JOURNALD_ACTIVE", "M2_JOURNALD_ACTIVE"),
    ("journald-log", "systemd-cat echo hello-journald-m2 && sleep 1 && journalctl -b --no-pager -n 20", "hello-journald-m2"),
    ("simpletest-stop", "systemctl stop simpletest && echo M2_SIMPLE_STOPPED && systemctl is-active simpletest || true", "M2_SIMPLE_STOPPED"),
]

PANIC_MARKERS = [
    b"kernel panic", b"Kernel panic", b"page fault handler failed",
    b"Oops", b"BUG:", b"panic!",
]


def uboot_commands() -> list[tuple[str, str, str]]:
    return [
        ("version", "version", "U-Boot 2026"),
        ("virtio-scan", "virtio scan", "=>"),
        ("filesystem", "ext4ls virtio 0:0 /", "asterinas.booti"),
        ("kernel-load", f"ext4load virtio 0:0 {KERNEL_LOAD:#x} /asterinas.booti", "bytes read"),
        ("dtb-load", f"ext4load virtio 0:0 {DTB_LOAD:#x} /qemu-virt.dtb", "bytes read"),
        ("dtb-select", f"fdt addr {DTB_LOAD:#x}", "Working FDT set"),
        ("bootargs", 'setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
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
                    f"timed out waiting for {needle!r}; tail={bytes(self.transcript[-800:])!r}"
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial-log", type=Path, default=Path("/tmp/asterinas-sd-m2-serial.log"))
    parser.add_argument("--collect-timeout", type=float, default=180.0)
    parser.add_argument("--smp", type=int, default=1)
    args = parser.parse_args()

    if not UBOOT.exists():
        raise SystemExit(f"missing U-Boot: {UBOOT}")
    if not BOOT_DISK.exists():
        raise SystemExit(f"missing boot disk: {BOOT_DISK}")

    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", str(args.smp),
        "-display", "none",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", str(UBOOT),
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
    ]

    boot = Boot(argv, args.serial_log)
    results: list[tuple[str, bool]] = []
    ended = "ok"
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

        print("[boot] waiting for /init launcher", flush=True)
        boot.read_until(INIT_MARKER, 120)
        print("[ok] /init reached", flush=True)

        print("[getty] waiting for login prompt", flush=True)
        boot.read_until(LOGIN_PROMPT, args.collect_timeout)
        print("[ok] getty login prompt seen", flush=True)
        results.append(("getty-login-prompt", True))

        boot.send("root")
        boot.read_until(SHELL_READY, 60)
        print("[ok] root login shell ready", flush=True)
        results.append(("login-shell", True))

        for name, cmd, expect in PROBES:
            print(f"[probe] {name}", flush=True)
            boot.send(cmd)
            # The serial tty echoes the command line verbatim, so the marker
            # string (which is part of `cmd` via `&& echo MARKER`) shows up in
            # the echo too. Consume the echo first so the real match is on the
            # command's *output*, not on what we typed.
            try:
                boot.read_until(cmd.encode(), 15)
            except TimeoutError:
                pass  # echo may have been split/partial; fall through to output
            try:
                boot.read_until(expect.encode(), 30)
                results.append((name, True))
            except TimeoutError:
                results.append((name, False))
            boot._drain(0.5)
    except TimeoutError as e:
        ended = "timeout"
        print(f"[boot] {e}", flush=True)
    except RuntimeError:
        ended = "serial-closed"
    finally:
        boot.close()

    raw = bytes(boot.transcript)
    clean = ANSI_RE.sub(b"", raw)
    transcript = clean.decode("utf-8", "replace")
    panics = [m.decode() for m in PANIC_MARKERS if m in clean]
    unimplemented = transcript.count("Unimplemented syscall")

    print("\n=== SYSTEMD-BOOT-M2 result ===", flush=True)
    for name, ok in results:
        print(f"  {name}: {'OK' if ok else 'FAIL'}", flush=True)
    print(f"  collection-ended: {ended}", flush=True)
    print(f"  unimplemented-syscall lines: {unimplemented}", flush=True)
    if panics:
        print(f"  panic markers: {panics}", flush=True)

    print("\n=== serial tail ===", flush=True)
    print(transcript[-6000:], flush=True)

    ok = all(r[1] for r in results) and len(results) == len(PROBES) + 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
