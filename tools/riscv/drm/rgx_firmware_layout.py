#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validate the selected Megrez PowerVR firmware's allocation table.

This is a read-only preflight for the pinned RockOS DDK's version-2 footer.
It does not decode the META LDR stream, load firmware, or prove GPU execution.
Only metadata and a digest are printed; licensed firmware stays out of Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys


FW_BLOCK_SIZE = 4096
HEADER = struct.Struct("<4IQ2I2HI")
ENTRY = struct.Struct("<6I")
EXPECTED_BVNC = (30 << 48) | (3 << 32) | (408 << 16) | 101
EXPECTED_DDK = (24, 2, 6643903)
SECTIONS = {
    0: (1, "code"),
    1: (2, "data"),
    2: (3, "coremem_code"),
    3: (4, "coremem_data"),
}


def _bvnc_text(value: int) -> str:
    return ".".join(str((value >> shift) & 0xFFFF) for shift in (48, 32, 16, 0))


def inspect_firmware(image: bytes) -> dict[str, object]:
    if len(image) <= FW_BLOCK_SIZE:
        raise ValueError("firmware is shorter than its trailer block")
    footer = len(image) - FW_BLOCK_SIZE
    (
        version,
        header_size,
        count,
        entry_size,
        bvnc,
        page_size,
        flags,
        ddk_major,
        ddk_minor,
        ddk_build,
    ) = HEADER.unpack_from(image, footer)
    if version != 2 or header_size != HEADER.size:
        raise ValueError("unsupported firmware header version or size")
    if not 1 <= count <= 8 or entry_size != ENTRY.size:
        raise ValueError("unsupported firmware layout table shape")
    if header_size + count * entry_size > FW_BLOCK_SIZE:
        raise ValueError("firmware layout table exceeds its trailer block")
    if bvnc != EXPECTED_BVNC:
        raise ValueError(f"PowerVR BVNC mismatch: {_bvnc_text(bvnc)}")
    if (ddk_major, ddk_minor, ddk_build) != EXPECTED_DDK:
        raise ValueError("PowerVR DDK version mismatch")
    if page_size != 0:
        raise ValueError("selected META firmware has an unexpected page-size field")

    sections: list[dict[str, int | str]] = []
    allocations: dict[str, int] = {}
    seen: set[int] = set()
    address_ranges: list[tuple[int, int]] = []
    for index in range(count):
        section_id, section_type, base, max_size, size, offset = ENTRY.unpack_from(
            image, footer + header_size + index * entry_size
        )
        expected = SECTIONS.get(section_id)
        if expected is None or section_type != expected[0] or section_id in seen:
            raise ValueError("unexpected or duplicate META firmware section")
        if size == 0 or size > max_size or base + max_size > 1 << 32:
            raise ValueError("invalid META firmware section bounds")
        end = base + max_size
        if any(
            base < previous_end and previous_base < end
            for previous_base, previous_end in address_ranges
        ):
            raise ValueError("overlapping META firmware address ranges")
        address_ranges.append((base, end))
        seen.add(section_id)
        kind = expected[1]
        allocations[kind] = allocations.get(kind, 0) + size
        sections.append(
            {
                "id": section_id,
                "type": kind,
                "base": base,
                "max_size": max_size,
                "alloc_size": size,
                "alloc_offset": offset,
            }
        )
    if seen != set(SECTIONS):
        raise ValueError("selected META firmware is missing a required section")
    if any(
        section["alloc_offset"] + section["alloc_size"] > allocations[section["type"]]
        for section in sections
    ):
        raise ValueError("META firmware section exceeds its allocation")
    return {
        "sha256": hashlib.sha256(image).hexdigest(),
        "image_bytes": len(image),
        "bvnc": _bvnc_text(bvnc),
        "ddk": f"{ddk_major}.{ddk_minor}.{ddk_build}",
        "processor": "META",
        "fw_page_size": page_size,
        "flags": f"0x{flags:08x}",
        "allocation_bytes": allocations,
        "sections": sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    args = parser.parse_args()
    try:
        if args.firmware.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("firmware exceeds the 16 MiB preflight limit")
        result = inspect_firmware(args.firmware.read_bytes())
    except (OSError, ValueError) as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
