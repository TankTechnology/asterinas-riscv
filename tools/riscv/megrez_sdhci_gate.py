#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Classify and publish bounded Megrez SDHCI read-only evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROLLER = re.compile(r"\[mmc\] controller 0x50460000 irq=81 read-only")
_CARD = re.compile(
    r"\[mmc\] SDHC rca=(?P<rca>[1-9][0-9]*) sectors=(?P<sectors>[1-9][0-9]*)"
    r"(?: sector0=[0-9a-fA-F]{4})?"
)
_BLOCK = re.compile(r"\[mmc\] mmcblk0 registered read-only")
_PARTITION = re.compile(r"\[mmc\] partition-table sha256=(?P<sha256>[0-9a-f]{64})")
_FATAL = re.compile(
    r"(?i)(uncaught panic|\bpanic\b|\bfatal\b|\[mmc\] probe failed|IoError)"
)
_WRITABLE = re.compile(r"(?i)(write[- ]enabled|\bwritable\b|read-write)")


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    sectors: int | None = None
    partition_sha256: str | None = None


def _failure(reason: str) -> GateResult:
    return GateResult(False, reason)


def classify(transcript: bytes) -> GateResult:
    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        return _failure("transcript-too-large")
    text = _ANSI.sub("", transcript.decode("utf-8", errors="replace"))
    if _FATAL.search(text):
        return _failure("fatal-marker")
    if _WRITABLE.search(text):
        return _failure("write-enabled-marker")

    matches = [
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

    card = matches[1][0]
    sectors = int(card.group("sectors"))
    if sectors <= 0:
        return _failure("invalid-capacity")
    return GateResult(
        True,
        "passed",
        sectors=sectors,
        partition_sha256=matches[3][0].group("sha256"),
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def publish(transcript: bytes, output_dir: Path) -> GateResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = classify(transcript)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = publish(args.transcript.read_bytes(), args.output_dir)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
