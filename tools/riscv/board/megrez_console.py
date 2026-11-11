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
import hashlib
import os
import re
import select
import sys
import termios
import threading
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
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


# U-Boot's hush and POSIX shells both expand an unset `${VAR}` to nothing, so
# the executed line prints the marker while the echoed command line keeps the
# unexpanded form. That keeps the marker out of the echo.
_RUN_MARKER_VAR = "__ASTERINAS_RUN_MARKER__"


def run_command_line(command: str, marker: str) -> str:
    """Builds a shell line whose echo cannot contain `marker`."""
    suffix = marker[len("ZZOK") :]
    return f"{command}; echo ZZOK${{{_RUN_MARKER_VAR}}}{suffix}"


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
        """Type a command and collect the console text up to its marker.

        The marker is assembled by the shell (`ZZOK${VAR}1234`), not typed
        literally, so the echo of the command line cannot match it. Matching
        the echo would return before the command has run, which silently
        turns output checks — command output, byte counts, register reads —
        into no-ops.
        """
        # A per-call marker: the bridge appends to the log asynchronously, so a
        # previous command's marker can land inside this call's window and
        # satisfy the wait before the new command has run.
        self._run_sequence = getattr(self, "_run_sequence", 0) + 1
        marker = f"ZZOK{os.getpid()}x{self._run_sequence}"
        # Give the previous command's last bytes a moment to land so they
        # cannot leak into this command's transcript window.
        time.sleep(0.3)
        offset = os.path.getsize(self.log)
        self.type_line(run_command_line(command, marker), char_pause)
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
        marker = f"ZZPUSH{os.getpid()}"
        offset = os.path.getsize(self.log)
        # Do not push through a heredoc: bash reads heredoc lines through its
        # own line handling, which drops tabs (readline-style completion) and
        # mangles other control bytes. `cat` reads the bytes verbatim.
        #
        # The command is terminated with LF, not CRLF: the bridge only flushes
        # its FIFO buffer on LF, and that single LF both flushes the buffer and
        # ends the command line. CRLF would leave an extra empty line in the
        # file because cat is already reading by then.
        self.send_line("!RAW 1")
        time.sleep(0.4)
        for char in f"cat > {remote}":
            self.send_raw(char.encode())
            time.sleep(DEFAULT_CHAR_PAUSE)
        self.send_raw(b"\n")
        time.sleep(0.4)
        self.send_line(f"!PACE {pace_ms}")
        time.sleep(0.4)
        self.send_line("!RAW 1")
        time.sleep(0.4)
        self.send_raw(payload)
        # An empty line is EOF in canonical mode. A payload that ends without
        # a newline first needs one ^D to flush the partial line.
        self.send_raw(b"\x04")
        if not payload.endswith(b"\n"):
            time.sleep(0.3)
            self.send_raw(b"\x04")
        time.sleep(0.3)
        self.send_line("!RAW 0")
        self.send_line(f"!PACE {TYPING_PACE_MS}")
        time.sleep(0.5)
        start = os.path.getsize(self.log)
        self.type_line(f"echo {marker}")
        text = ""
        offset = start
        # The bridge writes the payload at the requested pace, so the marker
        # cannot arrive before the transfer itself has had time to finish.
        transfer_seconds = len(payload) * pace_ms / 1000.0
        deadline = time.time() + 30 + transfer_seconds
        while time.time() < deadline:
            time.sleep(0.5)
            chunk, offset = self.read_since(offset)
            text += chunk
            if re.search(rf"^{marker}\r?$", text, re.MULTILINE):
                return True
        return False

    def remote_digest(self, remote: str, timeout: float = 30.0) -> str | None:
        """Return the guest's sha256 of `remote`, or None if it did not answer."""

        start = os.path.getsize(self.log)
        self.type_line(
            f"echo R_DIGEST_BEGIN; sha256sum {remote} 2>/dev/null; echo R_DIGEST_END"
        )
        text = ""
        offset = start
        end = time.time() + timeout
        while time.time() < end:
            chunk, offset = self.read_since(offset)
            text += chunk
            # The echoed command contains the marker too; only a marker alone
            # on its line means the guest has finished computing the digest.
            if re.search(r"^R_DIGEST_END\r?$", text, re.MULTILINE):
                break
            time.sleep(0.3)
        for match in re.finditer(r"R_DIGEST_BEGIN(.*?)R_DIGEST_END", text, re.S):
            digest = re.search(r"\b([0-9a-f]{64})\b", match.group(1))
            if digest:
                return digest.group(1)
        return None

    def push_verified(
        self, local: str, remote: str, paces: tuple[float, ...] = (2.0, 6.0, 12.0)
    ) -> bool:
        """Push a file and require the guest's digest to match the host's.

        The console has no flow control and drops bytes under load, so a
        successful push marker proves nothing about the file. Retry at slower
        paces until the content matches.
        """

        with open(local, "rb") as source:
            expected = hashlib.sha256(source.read()).hexdigest()
        for pace in paces:
            if not self.push(local, remote, pace_ms=pace):
                continue
            if self.remote_digest(remote) == expected:
                return True
        return False


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


class BootSafetyError(ValueError):
    """The planned boot would leave the board unreachable without hands."""


# `interactive` only accepts a rootfs labelled `ASTER_DEBIANROOT`. Our images
# are labelled `ASTER_BROWSERWEB`, and a mismatch ends in stage1's
# `fail_and_hold()`: no shell, no signal handling, no reboot. The basic
# initramfs shell plus a chroot covers the same need without the precondition.
_BOOT_SAFE_ROOT_INIT_MODES = frozenset({"basic", "systemd", "probe", "probe-auto"})
_MAX_REBOOT_AFTER_SECONDS = 24 * 3600


def _initramfs_entries(path: str) -> set[str]:
    """Returns the paths inside a newc cpio archive (optionally gzip'd)."""
    import gzip

    with open(path, "rb") as stream:
        data = stream.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    entries: set[str] = set()
    offset = 0
    while offset + 110 <= len(data) and data[offset : offset + 6] in (
        b"070701",
        b"070702",
    ):
        def field(at: int) -> int:
            return int(data[offset + at : offset + at + 8], 16)

        size = field(54)
        name_size = field(94)
        name_start = offset + 110
        name = data[name_start : name_start + name_size - 1].decode("utf-8", "replace")
        if name == "TRAILER!!!":
            break
        entries.add(name.removeprefix("./").lstrip("/"))
        offset = name_start + name_size
        offset = (offset + 3) & ~3
        offset = (offset + size + 3) & ~3
    return entries


def validate_initramfs_for_mode(
    mode: str, initramfs_path: str | None
) -> None:
    """Refuses a mode the loaded initramfs cannot satisfy.

    `basic` needs `/bin/busybox` in the initramfs. The deployed Megrez
    initramfs does not contain one, and booting `basic` against it ends in
    stage1's `fail_and_hold("basic-busybox-missing")` — the board is then
    unreachable until the boot deadline fires. The archive is checked from a
    local copy because U-Boot cannot read it.
    """

    if mode != "basic":
        return
    if not initramfs_path:
        raise BootSafetyError(
            "basic mode requires --initramfs-cpio with a local copy of the "
            "initramfs the board will load, so its busybox can be checked"
        )
    entries = _initramfs_entries(initramfs_path)
    if "bin/busybox" not in entries and "usr/bin/busybox" not in entries:
        raise BootSafetyError(
            f"{initramfs_path} has no /bin/busybox; --root-init=basic would "
            "hold the board in fail_and_hold. Use --root-init=systemd"
        )

# The DesignWare watchdog U-Boot arms before a physical boot. It is the only
# recovery layer that survives a kernel that stops taking timer interrupts, so
# an ad-hoc boot must arm it the same way instead of relying on the persistent
# bootcmd's inline `mw.l` sequence.
_WATCHDOG_COMPONENT_OFFSET = 0x5080_00FC
_WATCHDOG_CONTROL = 0x5080_0000
_WATCHDOG_TIMEOUT_RANGE = 0x5080_0004
_WATCHDOG_COUNTER_RESTART = 0x5080_000C
_WATCHDOG_COMPONENT_TYPE = 0x4457_0120
_WATCHDOG_ARM_SEQUENCE = (
    "if itest.l *0x508000fc == 0x44570120; then "
    "mw.l 0x50800004 0xf; mw.l 0x5080000c 0x76; mw.l 0x50800000 0x1f; fi"
)
# The watchdog window is not readable until its clock/power gate is opened;
# the persistent bootcmd writes this before its `itest.l` component check.
_WATCHDOG_CLOCK_ENABLE = "mw.l 0x51828444 0x1"


def _read_md_word(text: str, address: int) -> int | None:
    """Extracts the first word of `md.l <address> 1` from a U-Boot transcript."""
    match = re.search(rf"{address:08x}:\s*([0-9a-fA-F]{{8}})", text)
    return int(match.group(1), 16) if match else None


def arm_hardware_watchdog(con, step_timeout: float, allow_missing: bool) -> None:
    """Arms and verifies the SoC watchdog before handing off to the kernel.

    U-Boot's persistent `bootcmd` arms this watchdog inline; an ad-hoc boot
    that just loads images and sends `booti` would skip it. Before that gap is
    closed, a kernel hang that also stops the software deadline (interrupts
    masked on the only hart with a timer) leaves no recovery layer at all.
    """

    text, ok = con.run(_WATCHDOG_CLOCK_ENABLE, step_timeout)
    if not ok:
        print(text[-800:], file=sys.stderr)
        raise SystemExit("refusing to boot: could not enable the watchdog clock")

    text, ok = con.run(f"md.l {_WATCHDOG_COMPONENT_OFFSET:#x} 1", step_timeout)
    component = _read_md_word(text, _WATCHDOG_COMPONENT_OFFSET)
    if not ok or component != _WATCHDOG_COMPONENT_TYPE:
        if allow_missing:
            print(
                "warning: the DesignWare watchdog was not detected; booting without it",
                file=sys.stderr,
            )
            return
        print(text[-800:], file=sys.stderr)
        raise SystemExit(
            "refusing to boot: the hardware watchdog was not detected "
            "(pass --allow-missing-watchdog for a platform without it)"
        )

    text, ok = con.run(_WATCHDOG_ARM_SEQUENCE, step_timeout)
    if not ok:
        print(text[-800:], file=sys.stderr)
        raise SystemExit("refusing to boot: the watchdog arm sequence failed")

    text, ok = con.run(f"md.l {_WATCHDOG_CONTROL:#x} 1", step_timeout)
    control = _read_md_word(text, _WATCHDOG_CONTROL)
    if not ok or control is None or control & 1 == 0:
        print(text[-800:], file=sys.stderr)
        raise SystemExit(
            "refusing to boot: the hardware watchdog is not enabled after arming"
        )


def validate_boot_append(
    append: str, *, allow_kernel_default: bool = False, allow_interactive: bool = False
) -> None:
    """Reject boot command lines that cannot recover from a hang.

    A board that hangs before userspace is unreachable from every software
    channel, so every ad-hoc boot must either carry a bounded
    `asterinas.reboot_after` deadline or run a kernel whose built-in default
    arms one. The check is deliberately strict: the 2026-11-11 wedge came from
    a command line with no deadline and an initramfs mode that never reached
    userspace.
    """

    tokens = append.split()
    if "--" not in tokens or tokens.count("--") != 1:
        raise BootSafetyError(
            "the command line must separate kernel and init options with exactly one '--'"
        )
    split = tokens.index("--")
    kernel_tokens, init_tokens = tokens[:split], tokens[split + 1 :]

    modes = [t.split("=", 1)[1] for t in init_tokens if t.startswith("--root-init=")]
    if len(modes) != 1:
        raise BootSafetyError("exactly one --root-init=<mode> is required")
    mode = modes[0]
    if mode == "interactive" and not allow_interactive:
        raise BootSafetyError(
            "interactive only matches the ASTER_DEBIANROOT label; this rootfs is "
            "ASTER_BROWSERWEB, so stage1 would hold forever. Use --root-init=basic"
        )
    if mode not in _BOOT_SAFE_ROOT_INIT_MODES and not allow_interactive:
        raise BootSafetyError(f"unknown --root-init mode {mode!r}")

    deadlines = [
        t.split("=", 1)[1]
        for t in kernel_tokens
        if t.startswith("asterinas.reboot_after=")
    ]
    if not deadlines:
        if not allow_kernel_default:
            raise BootSafetyError(
                "no asterinas.reboot_after deadline: a hang before userspace would "
                "need a physical reset (pass the flag, or --allow-kernel-default for "
                "a kernel with the built-in 900 s default)"
            )
        return
    try:
        seconds = int(deadlines[-1], 10)
    except ValueError as error:
        raise BootSafetyError(
            f"asterinas.reboot_after is not a decimal number: {deadlines[-1]!r}"
        ) from error
    if not 1 <= seconds <= _MAX_REBOOT_AFTER_SECONDS:
        raise BootSafetyError(
            f"asterinas.reboot_after={seconds} is outside 1..{_MAX_REBOOT_AFTER_SECONDS}"
        )


def cmd_boot(args: argparse.Namespace) -> int:
    """Load a p2 kernel plus the p1 initramfs and DTB, then hand off with booti."""
    try:
        validate_boot_append(
            args.append,
            allow_kernel_default=args.allow_kernel_default,
            allow_interactive=args.allow_interactive,
        )
        mode = next(
            token.split("=", 1)[1]
            for token in args.append.split("--", 1)[1].split()
            if token.startswith("--root-init=")
        )
        validate_initramfs_for_mode(mode, args.initramfs_cpio)
    except BootSafetyError as error:
        raise SystemExit(f"refusing to boot: {error}") from error

    con = Console(args.log, args.fifo)

    # A reset must always return to a bootable default. The persistent
    # bootcmd lives in SPI and survives everything short of eraseenv, so
    # checking it costs one command and rules out a board that can only be
    # recovered by reflashing.
    text, ok = con.run("printenv bootcmd", args.step_timeout)
    if not ok or "bootcmd=" not in text or "booti" not in text:
        print(text[-800:], file=sys.stderr)
        raise SystemExit("refusing to boot: U-Boot bootcmd is missing or not a booti boot")

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
        if command.endswith(args.kernel) and args.expect_kernel_size is not None:
            reads = re.findall(r"(\d+) bytes read", text)
            if not reads:
                print(text[-800:], file=sys.stderr)
                raise SystemExit(
                    "refusing to boot: the load output did not report the kernel size"
                )
            size = int(reads[-1])
            if size != args.expect_kernel_size:
                raise SystemExit(
                    f"refusing to boot: {args.kernel} is {size} bytes on the board, "
                    f"expected {args.expect_kernel_size}"
                )

    arm_hardware_watchdog(con, args.step_timeout, args.allow_missing_watchdog)

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
    boot.add_argument(
        "--allow-kernel-default",
        action="store_true",
        help="accept a command line without asterinas.reboot_after when the kernel "
        "carries the built-in 900 s recovery default",
    )
    boot.add_argument(
        "--allow-interactive",
        action="store_true",
        help="allow --root-init=interactive (only for a rootfs labelled ASTER_DEBIANROOT)",
    )
    boot.add_argument(
        "--expect-kernel-size",
        type=int,
        default=None,
        help="refuse to boot when the kernel image read from p2 has another size",
    )
    boot.add_argument(
        "--allow-missing-watchdog",
        action="store_true",
        help="boot even when the DesignWare watchdog is absent (for QEMU/virt)",
    )
    boot.add_argument(
        "--initramfs-cpio",
        default=None,
        help="local copy of the initramfs the board loads; required to prove "
        "that --root-init=basic has a busybox",
    )
    boot.set_defaults(func=cmd_boot)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
