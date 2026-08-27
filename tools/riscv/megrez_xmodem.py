#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Transfer one memory artifact to Megrez U-Boot with bounded XMODEM-1K."""

from __future__ import annotations

import argparse
import os
import re
import select
import stat
import sys
import termios
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

SOH = 0x01
STX = 0x02
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_REQUEST = ord("C")
BLOCK_SIZE = 1024
PAD = 0x1A
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 256 * 1024
INITIAL_BAUD = 115200
TRANSFER_BAUD = 1_500_000
BAUD_SETTLE_SECONDS = 1.0


class TransferError(RuntimeError):
    """One stable, bounded transfer failure."""


@dataclass(frozen=True)
class TransferResult:
    blocks: int
    retries: int


def crc16_xmodem(payload: bytes) -> int:
    crc = 0
    for byte in payload:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else crc << 1
    return crc


def build_packet(block_number: int, payload: bytes) -> bytes:
    if not 0 <= block_number <= 0xFF or len(payload) > BLOCK_SIZE:
        raise ValueError("invalid XMODEM block")
    padded = payload.ljust(BLOCK_SIZE, bytes((PAD,)))
    crc = crc16_xmodem(padded)
    return (
        bytes((STX, block_number, 0xFF - block_number))
        + padded
        + crc.to_bytes(2, "big")
    )


def send_payload(
    payload: bytes,
    *,
    read_control: Callable[[float], int | None],
    write_all: Callable[[bytes], None],
    response_timeout: float = 3.0,
    retry_limit: int = 10,
    start_received: bool = False,
) -> TransferResult:
    if not payload:
        raise TransferError("artifact is empty")
    if not start_received:
        start = read_control(response_timeout)
        if start == CAN:
            raise TransferError("transfer cancelled by receiver")
        if start != CRC_REQUEST:
            raise TransferError("timed out waiting for CRC start handshake")

    retries = 0
    blocks = (len(payload) + BLOCK_SIZE - 1) // BLOCK_SIZE
    for index in range(blocks):
        number = (index + 1) & 0xFF
        packet = build_packet(
            number, payload[index * BLOCK_SIZE : (index + 1) * BLOCK_SIZE]
        )
        failures = 0
        while True:
            write_all(packet)
            control = read_control(response_timeout)
            if control == ACK:
                break
            if control == CAN:
                raise TransferError("transfer cancelled by receiver")
            failures += 1
            retries += 1
            if failures > retry_limit:
                raise TransferError(f"block {number} exceeded retry limit")

    failures = 0
    while True:
        write_all(bytes((EOT,)))
        control = read_control(response_timeout)
        if control == ACK:
            return TransferResult(blocks=blocks, retries=retries)
        if control == CAN:
            raise TransferError("transfer cancelled by receiver")
        failures += 1
        retries += 1
        if failures > retry_limit:
            raise TransferError("EOT exceeded retry limit")


def _configure_serial(fd: int, baud: int) -> None:
    speeds = {
        INITIAL_BAUD: termios.B115200,
        TRANSFER_BAUD: termios.B1500000,
    }
    try:
        speed = speeds[baud]
    except KeyError as error:
        raise TransferError(f"unsupported serial baud: {baud}") from error
    attrs = termios.tcgetattr(fd)
    attrs[0] = termios.IGNPAR
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = speed
    attrs[5] = speed
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _write_all(fd: int, payload: bytes, timeout: float = 3.0) -> None:
    view = memoryview(payload)
    deadline = time.monotonic() + timeout
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransferError("serial write deadline expired")
        _, writable, _ = select.select([], [fd], [], remaining)
        if not writable:
            raise TransferError("serial write deadline expired")
        try:
            written = os.write(fd, view)
        except BlockingIOError:
            continue
        if written <= 0:
            raise TransferError("serial closed during write")
        view = view[written:]


def _read_control(fd: int, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], deadline - time.monotonic())
        if not readable:
            return None
        try:
            byte = os.read(fd, 1)
        except BlockingIOError:
            continue
        if not byte:
            raise TransferError("serial closed during transfer")
        if byte[0] in (CRC_REQUEST, ACK, NAK, CAN):
            return byte[0]
    return None


def _read_until(fd: int, marker: bytes, timeout: float) -> bytes:
    transcript = bytearray()
    deadline = time.monotonic() + timeout
    while marker not in transcript:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransferError(f"timed out waiting for {marker!r}")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            continue
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        if not chunk:
            raise TransferError("serial closed while waiting for U-Boot")
        transcript.extend(chunk)
        if len(transcript) > MAX_TRANSCRIPT_BYTES:
            raise TransferError("U-Boot transcript exceeds 256 KiB")
    return bytes(transcript)


def _read_artifact(path: Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise TransferError(f"cannot inspect artifact: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TransferError("artifact must be a regular non-symlink file")
    if not 0 < info.st_size <= MAX_ARTIFACT_BYTES:
        raise TransferError("artifact size is outside the 1..64 MiB limit")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise TransferError(f"cannot open artifact: {error}") from error
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise TransferError("artifact identity changed before open")
        data = bytearray()
        while len(data) <= MAX_ARTIFACT_BYTES:
            chunk = os.read(fd, min(1024 * 1024, MAX_ARTIFACT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != opened.st_size or not data:
            raise TransferError("artifact size changed while reading")
        return bytes(data)
    finally:
        os.close(fd)


def _read_prompt(fd: int, timeout: float) -> bytes:
    transcript = bytearray()
    deadline = time.monotonic() + timeout
    while re.search(rb"(?:^|[\r\n])=> $", transcript) is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransferError("timed out waiting for U-Boot prompt")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            continue
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        if not chunk:
            raise TransferError("serial closed while waiting for U-Boot prompt")
        transcript.extend(chunk)
        if len(transcript) > MAX_TRANSCRIPT_BYTES:
            raise TransferError("U-Boot transcript exceeds 256 KiB")
    return bytes(transcript)


def _enter_transfer_mode(
    fd: int, address: int, *, current_baud: int = INITIAL_BAUD
) -> None:
    if current_baud == INITIAL_BAUD:
        command = f"loadx 0x{address:x} {TRANSFER_BAUD}\r".encode()
    elif current_baud == TRANSFER_BAUD:
        command = f"loadx 0x{address:x}\r".encode()
    else:
        raise TransferError(f"unsupported current serial baud: {current_baud}")
    _write_all(fd, command)
    if current_baud == INITIAL_BAUD:
        _read_until(fd, b"press ENTER", 10.0)
        _configure_serial(fd, TRANSFER_BAUD)
        time.sleep(BAUD_SETTLE_SECONDS)
        _write_all(fd, b"\r")
    start = _read_control(fd, 10.0)
    if start == CAN:
        raise TransferError("transfer cancelled by receiver")
    if start != CRC_REQUEST:
        raise TransferError("timed out waiting for CRC start handshake")


def _leave_transfer_mode(fd: int) -> None:
    _configure_serial(fd, INITIAL_BAUD)
    time.sleep(BAUD_SETTLE_SECONDS)
    _write_all(fd, bytes((0x1B,)))
    _read_prompt(fd, 10.0)


def _finish_transfer(
    fd: int, *, payload_size: int, address: int, current_baud: int
) -> None:
    if current_baud == INITIAL_BAUD:
        completion = _read_until(fd, b"press ESC", 10.0)
    else:
        completion = _read_prompt(fd, 10.0)
    size_match = re.search(
        rb"Total Size\s*=\s*0x[0-9a-fA-F]+\s*=\s*(\d+) Bytes", completion
    )
    address_match = re.search(rb"Start Addr\s*=\s*0x([0-9a-fA-F]+)", completion)
    if size_match is None or int(size_match.group(1)) != payload_size:
        raise TransferError("U-Boot reported a different transfer size")
    if address_match is None or int(address_match.group(1), 16) != address:
        raise TransferError("U-Boot reported a different start address")
    if current_baud == INITIAL_BAUD:
        _leave_transfer_mode(fd)


def _address(value: str) -> int:
    if re.fullmatch(r"(?:0x)?[0-9a-fA-F]{1,16}", value) is None:
        raise argparse.ArgumentTypeError("address must be hexadecimal")
    address = int(value, 16)
    if address == 0 or address % 4:
        raise argparse.ArgumentTypeError("address must be nonzero and 4-byte aligned")
    return address


def _transfer_payload_fd(
    fd: int, payload: bytes, address: int, *, current_baud: int
) -> TransferResult:
    _configure_serial(fd, current_baud)
    termios.tcflush(fd, termios.TCIFLUSH)
    _enter_transfer_mode(fd, address, current_baud=current_baud)
    result = send_payload(
        payload,
        read_control=lambda timeout: _read_control(fd, timeout),
        write_all=lambda data: _write_all(fd, data),
        start_received=True,
    )
    _finish_transfer(
        fd,
        payload_size=len(payload),
        address=address,
        current_baud=current_baud,
    )
    return result


def transfer_fd(
    fd: int,
    artifact: Path,
    address: int,
    *,
    current_baud: int = INITIAL_BAUD,
) -> TransferResult:
    """Transfer through a caller-owned descriptor without closing it."""

    payload = _read_artifact(artifact)
    return _transfer_payload_fd(fd, payload, address, current_baud=current_baud)


def transfer(
    device: str,
    artifact: Path,
    address: int,
    *,
    current_baud: int = INITIAL_BAUD,
) -> TransferResult:
    payload = _read_artifact(artifact)
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        return _transfer_payload_fd(fd, payload, address, current_baud=current_baud)
    finally:
        os.close(fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--current-baud",
        type=int,
        choices=(INITIAL_BAUD, TRANSFER_BAUD),
        default=INITIAL_BAUD,
    )
    parser.add_argument("device")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("address", type=_address)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        values = _parser().parse_args(arguments)
        result = transfer(
            values.device,
            values.artifact,
            values.address,
            current_baud=values.current_baud,
        )
    except (TransferError, OSError) as error:
        print(f"megrez-xmodem: {error}", file=sys.stderr)
        return 2
    print(f"MEGREZ_XMODEM_PASS blocks={result.blocks} retries={result.retries}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
