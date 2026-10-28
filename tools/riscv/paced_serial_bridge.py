#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Serial console bridge for the Megrez board: exclusive open, RX log, paced TX.

The board's U-Boot drops characters when a whole line is written at once, and a
session that is not logged cannot be audited afterwards.  This bridge is the
only owner of the UART: it opens the device exclusively, appends every received
byte to a log file and to stdout, and sends stdin to the board one character at
a time with a delay in between.

Usage:

    python3 tools/riscv/paced_serial_bridge.py /dev/ttyUSB0 /tmp/megrez-session.serial.log

Options:

    --baud 115200     line rate (the board's U-Boot uses 115200 8N1)
    --char-delay 0.008  seconds between transmitted characters
    --no-timestamps   log received bytes without a timestamp prefix

Exit with Ctrl-] (0x1d), the same key screen uses to detach.

Only the Python standard library is used: the host does not need pyserial, and
the bridge must keep working on a machine where the only thing installed is a
Python 3 interpreter.
"""

import argparse
import errno
import fcntl
import os
import select
import sys
import termios
import time
import tty

EXIT_KEY = b"\x1d"  # Ctrl-]

# The rates a board console realistically uses; termios wants a constant, not a
# number, so anything else is rejected rather than silently mis-set.
BAUDS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


def open_exclusive(path: str, baud: int) -> int:
    """Open the serial device so that no other session can share it."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_EXCL)
    attrs = termios.tcgetattr(fd)
    # Raw 8N1, no flow control, no echo: the board is the only one that
    # interprets these bytes.
    attrs[0] = 0  # iflag
    attrs[1] = 0  # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
    attrs[3] = 0  # lflag
    speed = BAUDS.get(baud)
    if speed is None:
        raise SystemExit(f"[bridge] unsupported baud {baud}; use one of {sorted(BAUDS)}")
    attrs[4] = attrs[5] = speed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    # O_EXCL alone does not make a terminal exclusive; TIOCEXCL does.
    fcntl.ioctl(fd, termios.TIOCEXCL)
    return fd


def make_raw_stdin() -> int:
    """Put the terminal in raw mode so keystrokes are forwarded as typed."""
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        return fd
    old = termios.tcgetattr(fd)

    def restore() -> None:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    import atexit

    atexit.register(restore)
    tty.setraw(fd)
    return fd


def run(port: str, log_path: str, baud: int, char_delay: float, timestamps: bool) -> int:
    serial_fd = open_exclusive(port, baud)
    stdin_fd = make_raw_stdin()
    log = open(log_path, "ab", buffering=0)
    last_was_newline = True

    print(f"[bridge] {port} @ {baud} 8N1, log -> {log_path}, exit with Ctrl-]", file=sys.stderr)

    try:
        while True:
            ready, _, _ = select.select([serial_fd, stdin_fd], [], [], 1.0)

            if serial_fd in ready:
                try:
                    chunk = os.read(serial_fd, 4096)
                except OSError as error:
                    if error.errno == errno.EIO:  # device unplugged
                        print("\n[bridge] serial device went away", file=sys.stderr)
                        return 1
                    raise
                if chunk:
                    if timestamps:
                        # Timestamp at the start of each line only, so the
                        # transcript stays readable and still comparable.
                        for byte in chunk:
                            if last_was_newline:
                                log.write(f"[{time.strftime('%H:%M:%S')}] ".encode())
                            log.write(bytes([byte]))
                            last_was_newline = byte == 0x0A
                    else:
                        log.write(chunk)
                    os.write(sys.stdout.fileno(), chunk)

            if stdin_fd in ready:
                typed = os.read(stdin_fd, 1024)
                if not typed:
                    return 0
                for byte in typed:
                    if bytes([byte]) == EXIT_KEY:
                        print("\n[bridge] detaching (port left free)", file=sys.stderr)
                        return 0
                    os.write(serial_fd, bytes([byte]))
                    time.sleep(char_delay)
    finally:
        log.close()
        os.close(serial_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("log", help="file receiving everything the board prints")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--char-delay",
        type=float,
        default=0.008,
        help="seconds between transmitted characters (default 8 ms)",
    )
    parser.add_argument(
        "--no-timestamps",
        action="store_true",
        help="log received bytes without per-line timestamps",
    )
    args = parser.parse_args()

    if not os.path.exists(args.port):
        print(f"[bridge] {args.port} does not exist; plug the USB-UART in", file=sys.stderr)
        return 2
    return run(args.port, args.log, args.baud, args.char_delay, not args.no_timestamps)


if __name__ == "__main__":
    sys.exit(main())
