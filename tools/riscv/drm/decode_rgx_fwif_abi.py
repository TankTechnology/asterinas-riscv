#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Decode the .rgx_fwif_abi section emitted by rgx_fwif_abi_probe.c."""

import argparse
import json
from pathlib import Path
import struct


ENTRY = struct.Struct("<64sQ")


def decode_layout(raw: bytes) -> dict[str, int]:
    if not raw or len(raw) % ENTRY.size:
        raise ValueError("invalid PowerVR ABI section length")
    layout = {}
    for name_raw, value in ENTRY.iter_unpack(raw):
        name_bytes, separator, padding = name_raw.partition(b"\0")
        if not separator or any(padding):
            raise ValueError("invalid PowerVR ABI entry name")
        name = name_bytes.decode("ascii")
        if not name or name in layout:
            raise ValueError("empty or duplicate PowerVR ABI entry name")
        layout[name] = value
    return layout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", type=Path, help="objcopy -O binary --only-section=.rgx_fwif_abi output")
    args = parser.parse_args()
    print(json.dumps(decode_layout(args.section.read_bytes()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
