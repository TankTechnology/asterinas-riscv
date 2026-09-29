#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Prepare the pinned Megrez META bootloader data without starting the GPU.

This reproduces the selected RockOS DDK's RGXProcessFWImage META branch for
two firmware threads, SLC_VIVT, and META_DMA. Its output is a root-private
staging input, not evidence that the GPU has read or executed the firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys

if __package__:
    from .rgx_firmware_layout import inspect_firmware
    from .rgx_meta_ldr import _prepare_ldr, _stage_buffers
else:
    from rgx_firmware_layout import inspect_firmware
    from rgx_meta_ldr import _prepare_ldr, _stage_buffers


EXPECTED_FIRMWARE_SHA256 = "25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b"
FIRMWARE_VADDRS = (0xE1C0000000, 0xE1C000E000, 0xE1C0014000, 0xE1C0027000)
BOOT_CONFIG_OFFSET = 0x80 * 4
BOOTLOADER_PAGE_SIZE = 4096
FW_HEAP_BASE = FIRMWARE_VADDRS[0]
META_THREADS = 2


def _cache_pairs() -> list[tuple[int, int]]:
    # rgxfwimageutils.c:RGXFWConfigureMetaCaches, with ui32NumThreads == 2.
    # The selected BVNC's MTP219, 96 KiB coremem, and pipeline version 0
    # enable RGXFW_META_SUPPORT_2ND_THREAD in rgxdefs_km.h.
    return [
        (0x04830600, (3 << 14) | (3 << 6)),
        (0x04830200, 0x80000007),
        (0x04830208, 0x80080007),
        (0x04830210, 0x80000000),
        (0x04830218, 0x80000000),
        (0x04830018, 1),
        (0x04830220, 7),
        (0x04830228, 0x80007),
        (0x04830230, 0),
        (0x04830238, 0),
        (0x04830020, 1),
        (0x040000C0, 0),
    ]


def prepare_meta_boot(
    image: bytes,
    *,
    expected_firmware_sha256: str = EXPECTED_FIRMWARE_SHA256,
    expected_ldr_writes: int = 17,
    firmware_vaddrs: tuple[int, int, int, int] = FIRMWARE_VADDRS,
) -> tuple[dict[str, object], dict[str, bytearray]]:
    firmware_sha = hashlib.sha256(image).hexdigest()
    if firmware_sha != expected_firmware_sha256:
        raise ValueError("selected META firmware SHA-256 mismatch")
    if tuple(firmware_vaddrs) != FIRMWARE_VADDRS:
        raise ValueError("selected GPU firmware VA layout mismatch")

    layout = inspect_firmware(image)
    ldr_summary, buffers, ldr_pairs, code_writes = _prepare_ldr(image)
    if len(ldr_pairs) != expected_ldr_writes:
        raise ValueError("selected META LDR config count mismatch")
    data = next(section for section in layout["sections"] if section["type"] == "data")
    code_size = len(buffers["code"])
    coremem_code_size = len(buffers["coremem_code"])

    # Pinned BVNC 30.3.408.101 has SLC_VIVT and META_DMA. The firmware-private
    # MMU context is 0. Only the data segment needs a new META segment mapping;
    # the bootloader code segment is configured by the GPU wrapper itself.
    seg_out = (3 << 52) | (firmware_vaddrs[1] + data["alloc_offset"])
    seg_base = data["base"] | (0xF << 8) | (1 << 1)
    seg_limit = max(4096, data["alloc_size"]) - 1
    pairs = [
        (0x04830030, 4),  # META_CR_SYSC_JTAG_THREAD: privileged accesses
        (0x04850010, seg_base),
        (0x04850014, seg_limit),
        (0x04850018, seg_out & 0xFFFFFFFF),
        (0x0485001C, seg_out >> 32),
        *ldr_pairs,
        *_cache_pairs(),
    ]

    # RGXSetFirmwareAddress selects META-cached, SLC-cached FW addresses for
    # RGX_FWCODEDATA_ALLOCFLAGS. The pinned feature row enables META_DMA.
    coremem_fw_addr = 0x10000000 + firmware_vaddrs[2] - FW_HEAP_BASE
    suffix = (0, 0, coremem_fw_addr, coremem_code_size,
              firmware_vaddrs[2] >> 32, firmware_vaddrs[2] & 0xFFFFFFFF)
    config = b"".join(struct.pack("<II", register, value) for register, value in pairs)
    config += struct.pack("<6I", *suffix)
    config_end = BOOT_CONFIG_OFFSET + len(config)
    if config_end > min(BOOTLOADER_PAGE_SIZE, code_size):
        raise ValueError("META boot configuration exceeds bootloader page")
    if any(start < config_end and end > BOOT_CONFIG_OFFSET for start, end in code_writes):
        raise ValueError("LDR code write overlaps META boot configuration")
    buffers["code"][BOOT_CONFIG_OFFSET:config_end] = config

    summary: dict[str, object] = {
        "firmware_sha256": firmware_sha,
        "processor": "META",
        "firmware_vaddrs": list(firmware_vaddrs),
        "meta_threads": META_THREADS,
        "slc_vivt": True,
        "meta_dma": True,
        "boot_config_offset": BOOT_CONFIG_OFFSET,
        "boot_config_pairs": len(pairs),
        "boot_config_ldr_writes": len(ldr_pairs),
        "boot_config_bytes": len(config),
        "boot_config_sha256": hashlib.sha256(config).hexdigest(),
        "segment_sha256": {
            kind: hashlib.sha256(buffer).hexdigest()
            for kind, buffer in sorted(buffers.items())
        },
        "ldr_blocks": ldr_summary["blocks"],
    }
    return summary, buffers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.firmware.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("firmware exceeds the 16 MiB preflight limit")
        summary, buffers = prepare_meta_boot(args.firmware.read_bytes())
        _stage_buffers(args.output_dir, buffers)
        manifest = args.output_dir / "manifest.json"
        try:
            descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(summary, stream, indent=2, sort_keys=True)
                stream.write("\n")
        except Exception:
            for path in args.output_dir.iterdir():
                path.unlink()
            args.output_dir.rmdir()
            raise
    except (OSError, ValueError) as error:
        parser.error(str(error))
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
