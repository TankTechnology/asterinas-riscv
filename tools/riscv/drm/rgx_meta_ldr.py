#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bound-check the selected PowerVR META firmware's LDR command stream.

The pinned RockOS DDK uses ProcessLDRCommandStream for this META firmware.
This tool validates the input and destination ranges without executing GPU
register writes or publishing licensed firmware bytes.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import struct
import sys

if __package__:
    from .rgx_firmware_layout import FW_BLOCK_SIZE, inspect_firmware
else:
    from rgx_firmware_layout import FW_BLOCK_SIZE, inspect_firmware


LDR_HEADER = struct.Struct("<IIIHH")
L1_HEADER = struct.Struct("<HHI")
L2_HEADER = struct.Struct("<HH")
END_OF_CHAIN = 0xFFFFFFFF
GLOBAL_RANGE_BIT = 0x80000000
COREMEM_DATA_START = 0x82000000
MAX_L1_BLOCKS = 4096
COMMAND_NAMES = {0: "loadmem", 3: "start_threads", 4: "zeromem", 5: "config"}


def _l2_payload(image: bytes, pointer: int, payload_end: int) -> bytes:
    if pointer < LDR_HEADER.size or pointer + L2_HEADER.size > payload_end:
        raise ValueError("L2 pointer exceeds firmware payload")
    _, length = L2_HEADER.unpack_from(image, pointer)
    if length < 6 or pointer + length > payload_end:
        raise ValueError("L2 block exceeds firmware payload")
    return image[pointer + L2_HEADER.size : pointer + length - 2]


def _section_for_write(
    address: int, size: int, sections: list[dict[str, int | str]]
) -> tuple[str, int]:
    if size <= 0:
        raise ValueError("zero-length LDR memory write")
    for candidate in (address, address & ~GLOBAL_RANGE_BIT):
        for section in sections:
            base = section["base"]
            allocation_end = base + section["alloc_size"]
            if base <= candidate < allocation_end:
                if candidate + size > allocation_end:
                    raise ValueError(
                        f"LDR write exceeds its firmware allocation: "
                        f"address={address:#x} size={size}"
                    )
                return str(section["type"]), section["alloc_offset"] + candidate - base
    raise ValueError(f"LDR write address is outside firmware allocations: {address:#x} size={size}")


def _is_bounded_coremem_data_zero(
    address: int, size: int, sections: list[dict[str, int | str]]
) -> bool:
    data = next(section for section in sections if section["type"] == "coremem_data")
    return (
        size > 0
        and COREMEM_DATA_START <= address
        and address + size <= data["base"] + data["max_size"]
    )


def _prepare_ldr(
    image: bytes,
) -> tuple[
    dict[str, object], dict[str, bytearray], list[tuple[int, int]], list[tuple[int, int]]
]:
    layout = inspect_firmware(image)
    payload_end = len(image) - FW_BLOCK_SIZE
    if payload_end < LDR_HEADER.size:
        raise ValueError("firmware payload has no META LDR header")
    _, _, pointer, _, _ = LDR_HEADER.unpack_from(image)
    if pointer < LDR_HEADER.size or pointer + L1_HEADER.size > payload_end:
        raise ValueError("META LDR first L1 pointer is outside firmware payload")

    seen: set[int] = set()
    commands: Counter[str] = Counter()
    write_bytes: Counter[str] = Counter()
    buffers = {
        kind: bytearray(size) for kind, size in layout["allocation_bytes"].items()
    }
    boot_config_writes = 0
    boot_config_pairs: list[tuple[int, int]] = []
    code_writes: list[tuple[int, int]] = []
    skipped_coremem_data_zero_bytes = 0
    while pointer != END_OF_CHAIN:
        if pointer in seen:
            raise ValueError("META LDR L1 chain contains a cycle")
        if len(seen) >= MAX_L1_BLOCKS or pointer + L1_HEADER.size > payload_end:
            raise ValueError("META LDR L1 chain exceeds firmware payload")
        seen.add(pointer)
        command_word, length, next_pointer = L1_HEADER.unpack_from(image, pointer)
        if length < L1_HEADER.size or pointer + length > payload_end:
            raise ValueError("META LDR L1 block exceeds firmware payload")
        if command_word & 0x10:
            commands["comment"] += 1
            pointer = next_pointer
            continue
        command = command_word & 0xF
        name = COMMAND_NAMES.get(command)
        if name is None:
            raise ValueError(f"unsupported META LDR command {command}")
        commands[name] += 1

        if command in (0, 4):
            if length < 16:
                raise ValueError("META LDR memory command is missing arguments")
            target, operand = struct.unpack_from("<II", image, pointer + L1_HEADER.size)
            if command == 0:
                payload = _l2_payload(image, operand, payload_end)
                size = len(payload)
            else:
                size = operand
            if command == 4 and _is_bounded_coremem_data_zero(
                target, size, layout["sections"]
            ):
                skipped_coremem_data_zero_bytes += size
            else:
                try:
                    address = target if command == 0 else target & ~GLOBAL_RANGE_BIT
                    section, offset = _section_for_write(address, size, layout["sections"])
                except ValueError as error:
                    raise ValueError(f"L1={pointer:#x} {name}: {error}") from error
                buffers[section][offset : offset + size] = (
                    payload if command == 0 else bytes(size)
                )
                write_bytes[section] += size
                if section == "code":
                    code_writes.append((offset, offset + size))
        elif command == 5:
            if length < 12:
                raise ValueError("META LDR config command is missing its L2 pointer")
            (l2_pointer,) = struct.unpack_from("<I", image, pointer + L1_HEADER.size)
            data = _l2_payload(image, l2_pointer, payload_end)
            if len(data) % 12:
                raise ValueError("META LDR config data has a partial register write")
            for offset in range(0, len(data), 12):
                operation, register, value = struct.unpack_from("<III", data, offset)
                if operation != 2:
                    raise ValueError(f"unsupported META LDR config operation {operation}")
                boot_config_writes += 1
                boot_config_pairs.append((register, value))
        pointer = next_pointer

    summary = {
        "firmware_sha256": layout["sha256"],
        "processor": layout["processor"],
        "l1_start": LDR_HEADER.unpack_from(image)[2],
        "blocks": len(seen),
        "command_counts": dict(sorted(commands.items())),
        "write_bytes": dict(sorted(write_bytes.items())),
        "segment_sha256": {
            kind: hashlib.sha256(buffer).hexdigest()
            for kind, buffer in sorted(buffers.items())
        },
        "boot_config_writes": boot_config_writes,
        "skipped_coremem_data_zero_bytes": skipped_coremem_data_zero_bytes,
    }
    return summary, buffers, boot_config_pairs, code_writes


def scan_ldr(image: bytes) -> dict[str, object]:
    summary, _, _, _ = _prepare_ldr(image)
    return summary


def _stage_buffers(output_dir: Path, buffers: dict[str, bytearray]) -> None:
    output_dir.mkdir(mode=0o700)
    written: list[Path] = []
    try:
        output_dir.chmod(0o700)
        for kind, buffer in sorted(buffers.items()):
            path = output_dir / f"{kind}.bin"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                written.append(path)
                stream.write(buffer)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        output_dir.rmdir()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    parser.add_argument("--output-dir", type=Path, help="new private directory for prepared segments")
    args = parser.parse_args()
    try:
        if args.firmware.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("firmware exceeds the 16 MiB preflight limit")
        result, buffers, _, _ = _prepare_ldr(args.firmware.read_bytes())
        if args.output_dir is not None:
            _stage_buffers(args.output_dir, buffers)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
