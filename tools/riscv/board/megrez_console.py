#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Drive the Megrez serial console through a persistent, paced bridge.

`megrez_board_session.py` owns the port for gated boot sessions. This helper
covers the other half of board work: an always-on bridge that keeps a local
copy of the console in a log file plus a command FIFO, so a long interactive
session (measurements, file pushes, U-Boot pokes) can be scripted from several
short commands without reopening the port -- reopening resets the guest's line
discipline and loses whatever foreground job the shell was running.

The board's console has no flow control, which produces two failure modes:

* A burst arrives faster than the guest's line discipline drains it, and
  characters are silently dropped. Every command here is typed one character at
  a time, and file payloads are pushed at about 11 KiB/s. 0.09 ms/byte already
  corrupts a multi-KiB payload, so the push pace defaults to 0.15 ms/byte.
* The closing carriage return of a typed line is occasionally swallowed, so the
  command echoes but never runs. Every line is followed by a blank line, which
  the bridge sends as an extra CR.
* The USB adapter can be re-enumerated (unplugged, or reset along with the
  board). The path stays the same but the held descriptor goes silent, which
  looks like a dead board, so the bridge watches for that and reopens.

Usage:

    megrez_console.py bridge /dev/ttyUSB0 --log /tmp/board.log --fifo /tmp/board.cmd
    megrez_console.py run "ip -4 addr" --timeout 30
    megrez_console.py type "dmesg -n 1"
    megrez_console.py push ./script.py /tmp/script.py
    megrez_console.py wait --pattern 'Hit any key to stop autoboot'
    megrez_console.py boot --kernel /root/kernel-rel.img --append 'console=ttyS0 init=/init'
"""

from __future__ import annotations

import argparse
import os
import re
import select
import sys
import termios
import threading
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
EOF_MARKER = "ZZEOF"
# Fastest pace that survives repeated multi-KiB pushes on the 115200 line.
PUSH_PACE_MS = 0.15
# At or above this pace the bridge hands over one byte per write, which keeps
# the guest's line discipline from dropping characters while a line is typed.
TYPING_PACE_MS = 20.0
DEFAULT_CHAR_PAUSE = 0.03

BAUDS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
    460800: termios.B460800,
    921600: termios.B921600,
}


class Bridge:
    """Owns the serial port for the lifetime of the session."""

    def __init__(self, device: str, log_path: str, fifo: str, baud: int, pace_ms: float):
        self.device = device
        self.log_path = log_path
        self.fifo = fifo
        self.baud = baud
        self.pace = pace_ms / 1000.0
        self.raw = False
        self.sent = 0
        self.reported = 0
        self.report_every = 512 * 1024
        self.lock = threading.Lock()
        self.fd = self._open(baud)

    def _open(self, baud: int) -> int:
        if baud not in BAUDS:
            raise SystemExit(f"unsupported baud {baud}")
        fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = attrs[5] = BAUDS[baud]
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 1
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        return fd

    def _reopen_if_replaced(self) -> None:
        """Reopens the port when the adapter has been re-enumerated.

        A USB serial adapter that is unplugged, power-cycled or reset comes
        back as the same path but a different device. The old descriptor stays
        readable but never returns a byte again, which looks exactly like a
        board that stopped talking; it cost an afternoon of "the board is hung"
        before the kernel log showed the disconnect.
        """
        try:
            current = os.stat(self.device)
        except OSError:
            return
        try:
            held = os.fstat(self.fd)
        except OSError:
            held = None
        if held is not None and (held.st_rdev, held.st_ino) == (current.st_rdev, current.st_ino):
            return
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            self.fd = self._open(self.baud)
            self._note("[bridge] reopened " + self.device + " after re-enumeration")
        except OSError as error:
            self._note(f"[bridge] reopen failed: {error}")
            time.sleep(1.0)

    def _note(self, text: str) -> None:
        with open(self.log_path, "ab", buffering=0) as log:
            log.write(text.encode() + b"\n")

    def reader(self) -> None:
        checks = 0
        while True:
            checks += 1
            if checks % 50 == 0:
                self._reopen_if_replaced()
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
            except (OSError, ValueError):
                time.sleep(0.2)
                continue
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                time.sleep(0.2)
                continue
            if chunk:
                with open(self.log_path, "ab", buffering=0) as log:
                    log.write(chunk)

    def write(self, data: bytes) -> None:
        """Send bytes at a rate the guest can absorb.

        Writes go out in blocks and sleep in proportion to the block size:
        per-byte sleeps would mean twelve thousand wakeups a second at 115200,
        which on its own is enough to starve the reader.
        """
        block_size = 1 if self.pace >= 0.001 else 256
        with self.lock:
            fd = self.fd
        for start in range(0, len(data), block_size):
            pending = data[start : start + block_size]
            while pending:
                try:
                    written = os.write(fd, pending)
                except OSError:
                    time.sleep(0.05)
                    continue
                if written == 0:
                    break
                pending = pending[written:]
                if self.pace:
                    time.sleep(self.pace * written)
                self.sent += written
            if self.sent - self.reported >= self.report_every:
                self.reported = self.sent
                self._note(f"[bridge] sent {self.sent} bytes")

    def commands(self) -> None:
        """Reads command lines while keeping one reader open for good.

        Reopening the pipe after every command leaves a window where the old
        read end is closed but a writer has already opened against it, and that
        write dies with EPIPE. Opening the pipe O_RDWR pins a reader for the
        bridge's lifetime, so writers always find one.
        """
        fd = -1
        while fd < 0:
            try:
                fd = os.open(self.fifo, os.O_RDWR)
            except FileNotFoundError:
                time.sleep(0.5)
        pending = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                continue
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self.handle(line + b"\n")

    def handle(self, raw_line: bytes) -> None:
        line = raw_line.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("!PACE "):
            try:
                self.pace = float(line.split()[1]) / 1000.0
                self._note(f"[bridge] pace -> {line.split()[1]} ms")
            except ValueError:
                self._note("[bridge] bad pace command")
            return
        if line.startswith("!RAW "):
            self.raw = line.split()[1] == "1"
            self._note(f"[bridge] raw -> {self.raw}")
            return
        if self.raw:
            # Verbatim: a transfer sender writes its payload as ordinary file
            # bytes so the byte count on the wire matches the file it was given.
            self.write(raw_line)
        else:
            self.write(line.encode() + b"\r")


def run_bridge(device: str, log_path: str, fifo: str, baud: int, pace_ms: float) -> int:
    if os.path.exists(fifo):
        os.unlink(fifo)
    os.mkfifo(fifo)
    # The bridge needs the serial device (root), but every tool that talks to
    # the board runs as the invoking user. A root-owned pipe would make
    # Console.send_line fail with EACCES, so let anyone write to it.
    os.chmod(fifo, 0o666)
    bridge = Bridge(device, log_path, fifo, baud, pace_ms)
    threading.Thread(target=bridge.reader, daemon=True).start()
    bridge._note(f"[bridge] started on {device} @ {baud}")
    bridge.commands()
    return 0


class Console:
    """Client side of the bridge: writes commands, reads the transcript."""

    def __init__(self, log: str, fifo: str) -> None:
        self.log = log
        self.fifo = fifo

    def send_line(self, line: str) -> None:
        with open(self.fifo, "w") as fifo:
            fifo.write(line + "\n")

    def send_raw(self, data: bytes) -> None:
        with open(self.fifo, "wb") as fifo:
            fifo.write(data)

    def read_since(self, offset: int) -> tuple[str, int]:
        with open(self.log, "rb") as log:
            log.seek(offset)
            chunk = log.read()
        return chunk.decode("utf-8", "replace"), offset + len(chunk)

    def type_line(self, text: str, char_pause: float = DEFAULT_CHAR_PAUSE) -> None:
        self.send_line("!RAW 1")
        time.sleep(0.4)
        for char in text:
            self.send_raw(char.encode())
            time.sleep(char_pause)
        self.send_raw(b"\r\n")
        time.sleep(0.3)
        self.send_line("!RAW 0")
        time.sleep(0.3)
        self.send_line("\n")

    def run(self, command: str, timeout: float, char_pause: float = DEFAULT_CHAR_PAUSE) -> tuple[str, bool]:
        """Type a command and collect the console text up to its marker."""
        marker = f"ZZOK{os.getpid()}"
        offset = os.path.getsize(self.log)
        self.type_line(f"{command}; echo {marker}", char_pause)
        collected = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.4)
            chunk, offset = self.read_since(offset)
            collected += chunk
            if marker in collected:
                break
        return ANSI.sub("", collected.replace(marker, "")), marker in collected

    def push(self, local: str, remote: str, pace_ms: float) -> bool:
        payload = open(local, "rb").read()
        if EOF_MARKER.encode() in payload:
            raise SystemExit("payload contains the heredoc delimiter")
        marker = f"ZZPUSH{os.getpid()}"
        offset = os.path.getsize(self.log)
        self.type_line(f"cat > {remote} <<'{EOF_MARKER}'; echo {marker}")
        time.sleep(0.4)
        self.send_line(f"!PACE {pace_ms}")
        time.sleep(0.4)
        self.send_line("!RAW 1")
        time.sleep(0.4)
        self.send_raw(payload)
        time.sleep(0.3)
        self.send_raw((EOF_MARKER + "\r\n").encode())
        time.sleep(1.5)
        self.send_line("!RAW 0")
        self.send_line(f"!PACE {TYPING_PACE_MS}")
        collected = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.5)
            chunk, offset = self.read_since(offset)
            collected += chunk
            if marker in collected:
                break
        return marker in collected


def cmd_run(args: argparse.Namespace) -> int:
    con = Console(args.log, args.fifo)
    text, ok = con.run(args.text, args.timeout, args.char_pause)
    print(text, end="")
    if not ok:
        print("--- marker not seen; the line may have been truncated ---", file=sys.stderr)
    return 0 if ok else 1


def cmd_type(args: argparse.Namespace) -> int:
    Console(args.log, args.fifo).type_line(args.text, args.char_pause)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    ok = Console(args.log, args.fifo).push(args.local, args.remote, args.pace)
    print("pushed", args.local, "->", args.remote, "ok" if ok else "FAILED")
    return 0 if ok else 1


def cmd_wait(args: argparse.Namespace) -> int:
    pattern = re.compile(args.pattern)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        with open(args.log, "rb") as log:
            tail = log.read()[-args.window :]
        if pattern.search(ANSI.sub("", tail.decode("utf-8", "replace"))):
            return 0
        time.sleep(args.interval)
    print(f"timed out waiting for {args.pattern!r}", file=sys.stderr)
    return 1


def cmd_boot(args: argparse.Namespace) -> int:
    """Load a p2 kernel plus the p1 initramfs and DTB, then hand off with booti."""
    con = Console(args.log, args.fifo)
    steps = [
        f"ext4load mmc 1:1 0x88300000 {args.initrd}",
        "setenv initrd_size ${filesize}",
        f"ext4load mmc 1:1 0x88000000 {args.dtb}",
        f"ext4load mmc 1:2 0x80200000 {args.kernel}",
        f"setenv bootargs {args.append}",
    ]
    for command in steps:
        print("$", command, flush=True)
        text, ok = con.run(command, args.step_timeout)
        if not ok or "Failed" in text or "Does not exist" in text:
            print(text[-800:], file=sys.stderr)
            raise SystemExit(f"rejected: {command}")
    con.type_line("booti 0x80200000 0x88300000:${initrd_size} 0x88000000")
    print("booti sent", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    bridge = sub.add_parser("bridge", help="run the persistent serial bridge")
    bridge.add_argument("device")
    bridge.add_argument("--log", required=True)
    bridge.add_argument("--fifo", required=True)
    bridge.add_argument("--baud", type=int, default=115200)
    bridge.add_argument("--pace", type=float, default=TYPING_PACE_MS, help="ms per byte")
    bridge.set_defaults(func=lambda a: run_bridge(a.device, a.log, a.fifo, a.baud, a.pace))

    for name in ("run", "type", "push", "wait"):
        node = sub.add_parser(name)
        node.add_argument("--log", default="/tmp/board.log")
        node.add_argument("--fifo", default="/tmp/board.cmd")
        if name == "run":
            node.add_argument("text")
            node.add_argument("--timeout", type=float, default=60.0)
            node.add_argument("--char-pause", type=float, default=DEFAULT_CHAR_PAUSE)
            node.set_defaults(func=cmd_run)
        elif name == "type":
            node.add_argument("text")
            node.add_argument("--char-pause", type=float, default=DEFAULT_CHAR_PAUSE)
            node.set_defaults(func=cmd_type)
        elif name == "push":
            node.add_argument("local")
            node.add_argument("remote")
            node.add_argument("--pace", type=float, default=PUSH_PACE_MS)
            node.set_defaults(func=cmd_push)
        else:
            node.add_argument("pattern")
            node.add_argument("--timeout", type=float, default=300.0)
            node.add_argument("--interval", type=float, default=2.0)
            node.add_argument("--window", type=int, default=4000)
            node.set_defaults(func=cmd_wait)

    boot = sub.add_parser("boot", help="ext4load + booti from the U-Boot prompt")
    boot.add_argument("--log", default="/tmp/board.log")
    boot.add_argument("--fifo", default="/tmp/board.cmd")
    boot.add_argument("--kernel", default="/root/kernel-rel.img", help="path inside p2")
    boot.add_argument("--initrd", default="stage1-daily.cpio")
    boot.add_argument("--dtb", default="megrez-daily.dtb")
    boot.add_argument("--append", required=True)
    boot.add_argument("--step-timeout", type=float, default=120.0)
    boot.set_defaults(func=cmd_boot)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
