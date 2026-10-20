#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Create a hash-pinned, unpublished Megrez desktop-m4 boot plan."""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
from pathlib import Path


def _artifact(name: str, path: Path, load_address: int) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "name": name,
        "path": str(path.resolve()),
        "load_address": load_address,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "crc32": f"{binascii.crc32(payload) & 0xffffffff:08x}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--initramfs", type=Path, required=True)
    parser.add_argument("--megrez-dtb", type=Path, required=True)
    parser.add_argument("--root-image", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    values = parser.parse_args()
    rows = [
        _artifact("kernel", values.kernel, 0x80200000),
        _artifact("initramfs", values.initramfs, 0x83000000),
        _artifact("megrez_dtb", values.megrez_dtb, 0xF0000000),
        _artifact("root_image", values.root_image, 0),
    ]
    document = {
        "schema_version": 2,
        "profile": "debian-browser",
        "smp": 4,
        "sv39": True,
        "bootargs": (
            "console=ttyS0 loglevel=info init=/init "
            "asterinas.reboot_after=300 -- --root-init=systemd"
        ),
        "markers": [
            "Enter riscv_boot",
            "ASTERINAS_DEBUG_CONSOLE_READY uid=0",
            "ASTERINAS_DESKTOP_M4_READY user=asterinas display=:0",
        ],
        "artifacts": rows,
    }
    values.output.parent.mkdir(parents=True, exist_ok=True)
    values.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output": str(values.output), "root_image_sha256": rows[-1]["sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
