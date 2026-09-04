#!/usr/bin/env python3

# SPDX-License-Identifier: MPL-2.0

"""Fail-closed validation for the complete ``make run_kernel`` transcript."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


SUCCESS_MARKERS = {
    "boot": "Successfully booted.",
    "conformance": "All conformance tests passed.",
    "regression": "All regression tests passed.",
    "vsock": "Vsock test passed.",
}
FATAL_PATTERNS = (
    re.compile(r"uncaught panic", re.IGNORECASE),
    re.compile(r"kernel panic", re.IGNORECASE),
    re.compile(r"unexpected exception", re.IGNORECASE),
    re.compile(r"sbi remote fence\.i(?: to hart [0-9]+)? failed", re.IGNORECASE),
)
ICACHE_SMP4_MARKER = re.compile(
    r"^riscv_flush_icache cross-hart passed: "
    r"cpus=4 local=([0-9]+) remotes=([0-9]+),([0-9]+),([0-9]+) "
    r"generations=1024$"
)


class ValidationError(ValueError):
    """The transcript does not prove the requested acceptance result."""


def validate_transcript(
    transcript: str, *, mode: str, require_riscv_icache_smp4: bool = False
) -> None:
    marker = SUCCESS_MARKERS[mode]
    lines = transcript.splitlines()
    if sum(line == marker for line in lines) != 1:
        raise ValidationError(f"expected exactly one terminal marker: {marker}")

    for line in lines:
        for pattern in FATAL_PATTERNS:
            if pattern.search(line):
                raise ValidationError(f"fatal transcript marker: {line.strip()}")

    if not require_riscv_icache_smp4:
        return
    if mode != "regression":
        raise ValidationError("the SMP4 icache contract requires regression mode")
    if any("riscv_flush_icache cross-hart skipped" in line for line in lines):
        raise ValidationError("the SMP4 icache regression was skipped")

    matches = [ICACHE_SMP4_MARKER.fullmatch(line) for line in lines]
    matches = [match for match in matches if match is not None]
    if len(matches) != 1:
        raise ValidationError("expected exactly one SMP4 cross-hart icache marker")
    cpu_ids = tuple(int(value) for value in matches[0].groups())
    if len(set(cpu_ids)) != 4:
        raise ValidationError("SMP4 cross-hart icache marker has duplicate CPU IDs")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--mode", choices=tuple(SUCCESS_MARKERS), required=True)
    parser.add_argument("--require-riscv-icache-smp4", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        transcript = args.log.read_text(encoding="utf-8", errors="replace")
        validate_transcript(
            transcript,
            mode=args.mode,
            require_riscv_icache_smp4=args.require_riscv_icache_smp4,
        )
    except (OSError, ValidationError) as error:
        print(f"run_kernel validation failed: {error}")
        return 1
    print(f"run_kernel validation passed: mode={args.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
