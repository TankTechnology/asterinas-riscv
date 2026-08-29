#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Classify and publish bounded Megrez SDHCI read-only evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
READ_PROBE_BYTES = 32 * 1024 * 1024
SDMA_BUFFER_BYTES = 512 * 1024
SDMA_CPU_START = 0xC0000000
SDMA_CPU_END = 0x100000000

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BUFFER = re.compile(
    r"\[mmc\] SDMA buffer cpu=(?P<cpu>0x[0-9a-f]+) "
    r"device=(?P=cpu) bytes=524288"
)
_CONTROLLER = re.compile(r"\[mmc\] controller 0x50460000 irq=81 sdma boundary=524288")
_CARD = re.compile(
    r"\[mmc\] SDHC rca=(?P<rca>[1-9][0-9]*) sectors=(?P<sectors>[1-9][0-9]*)"
    r"(?: sector0=[0-9a-fA-F]{4})?"
)
_BLOCK = re.compile(r"\[mmc\] mmcblk0 registered read-only")
_PARTITION = re.compile(r"\[mmc\] partition-table sha256=(?P<sha256>[0-9a-f]{64})")
_UPTIME = r"(?:0|[1-9][0-9]*)\.[0-9]+"
_READ_START = re.compile(
    rf"MEGREZ_SDHCI_READ_START bytes=(?P<bytes>[0-9]+) "
    rf"uptime=(?P<uptime>{_UPTIME})"
)
_READ_PASS = re.compile(
    rf"MEGREZ_SDHCI_READ_PASS bytes=(?P<bytes>[0-9]+) "
    rf"crc32=(?P<crc32>[0-9a-f]{{8}}) start=(?P<start>{_UPTIME}) "
    rf"end=(?P<end>{_UPTIME})"
)
_FATAL = re.compile(
    r"(?i)(uncaught panic|\bpanic\b|\bfatal\b|\[mmc\] probe failed|"
    r"\[mmc\].*bounded-pio-fallback|IoError)"
)
_WRITABLE = re.compile(r"(?i)(write[- ]enabled|\bwritable\b|read-write)")


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    sectors: int | None = None
    partition_sha256: str | None = None
    read_bytes: int | None = None
    read_crc32: str | None = None
    elapsed_seconds: float | None = None


def _failure(reason: str) -> GateResult:
    return GateResult(False, reason)


def classify(transcript: bytes, *, expected_crc32: str | None = None) -> GateResult:
    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        return _failure("transcript-too-large")
    text = _ANSI.sub("", transcript.decode("utf-8", errors="replace"))
    if _FATAL.search(text):
        return _failure("fatal-marker")
    if _WRITABLE.search(text):
        return _failure("write-enabled-marker")

    matches = [
        list(_BUFFER.finditer(text)),
        list(_CONTROLLER.finditer(text)),
        list(_CARD.finditer(text)),
        list(_BLOCK.finditer(text)),
        list(_PARTITION.finditer(text)),
    ]
    if any(len(found) != 1 for found in matches):
        return _failure("missing-or-duplicate-marker")
    ordered = [found[0].start() for found in matches]
    if ordered != sorted(ordered):
        return _failure("out-of-order-marker")

    buffer_address = int(matches[0][0].group("cpu"), 16)
    if (
        buffer_address < SDMA_CPU_START
        or buffer_address + SDMA_BUFFER_BYTES > SDMA_CPU_END
        or buffer_address % SDMA_BUFFER_BYTES != 0
    ):
        return _failure("invalid-sdma-buffer")

    card = matches[2][0]
    sectors = int(card.group("sectors"))
    if sectors <= 0:
        return _failure("invalid-capacity")
    result = GateResult(
        True,
        "passed",
        sectors=sectors,
        partition_sha256=matches[4][0].group("sha256"),
    )
    if expected_crc32 is None:
        return result
    if re.fullmatch(r"[0-9a-f]{8}", expected_crc32) is None:
        return _failure("invalid-expected-crc32")

    start_matches = list(_READ_START.finditer(text))
    pass_matches = list(_READ_PASS.finditer(text))
    if len(start_matches) != 1 or len(pass_matches) != 1:
        return _failure("missing-or-duplicate-read-marker")
    start_match = start_matches[0]
    pass_match = pass_matches[0]
    if (
        matches[4][0].start() > start_match.start()
        or start_match.start() > pass_match.start()
    ):
        return _failure("out-of-order-read-marker")
    try:
        read_bytes = int(pass_match.group("bytes"))
        start = Decimal(start_match.group("uptime"))
        pass_start = Decimal(pass_match.group("start"))
        end = Decimal(pass_match.group("end"))
    except (InvalidOperation, ValueError):
        return _failure("invalid-read-marker")
    if (
        int(start_match.group("bytes")) != READ_PROBE_BYTES
        or read_bytes != READ_PROBE_BYTES
    ):
        return _failure("read-size-mismatch")
    if pass_match.group("crc32") != expected_crc32:
        return _failure("read-crc32-mismatch")
    if start != pass_start or end < start:
        return _failure("read-time-mismatch")
    return GateResult(
        True,
        "passed",
        sectors=sectors,
        partition_sha256=matches[4][0].group("sha256"),
        read_bytes=read_bytes,
        read_crc32=pass_match.group("crc32"),
        elapsed_seconds=float(end - start),
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def publish(
    transcript: bytes,
    output_dir: Path,
    *,
    expected_crc32: str | None = None,
) -> GateResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = classify(transcript, expected_crc32=expected_crc32)
    _atomic_write(output_dir / "serial.log", transcript)
    payload = (
        json.dumps(asdict(result), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    _atomic_write(output_dir / "result.json", payload)
    directory_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expected-crc32")
    args = parser.parse_args(arguments)
    result = publish(
        args.transcript.read_bytes(),
        args.output_dir,
        expected_crc32=args.expected_crc32,
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
