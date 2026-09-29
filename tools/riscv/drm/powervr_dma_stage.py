#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Stage checked PowerVR META segments into one root-only DMA control session."""

import argparse
import hashlib
import json
import os
from pathlib import Path

SEGMENTS = (("code", 52_064), ("data", 18_432), ("coremem_code", 73_312), ("coremem_data", 9_984))
EXPECTED_FIRMWARE_SHA256 = "25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[3] / "docs/porting/evidence/2026-09-30-megrez-powervr-fw-layout/ldr-scan.json"


def checked_frames(segments_dir: Path, manifest: Path) -> list[bytes]:
    record = json.loads(manifest.read_text())
    if record.get("processor") != "META" or record.get("firmware_sha256") != EXPECTED_FIRMWARE_SHA256:
        raise ValueError("firmware manifest identity mismatch")
    digests = record.get("segment_sha256")
    if not isinstance(digests, dict):
        raise ValueError("firmware manifest lacks segment digests")
    frames = []
    for segment, (name, size) in enumerate(SEGMENTS):
        path = segments_dir / f"{name}.bin"
        if path.stat().st_size != size:
            raise ValueError(f"{name} size mismatch")
        payload = path.read_bytes()
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digests.get(name):
            raise ValueError(f"{name} hash mismatch")
        frames.append(b"PVR1" + segment.to_bytes(4, "little") + size.to_bytes(4, "little") + payload)
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--device", type=Path, default=Path("/dev/powervr-control"))
    args = parser.parse_args()
    try:
        frames = checked_frames(args.segments_dir, args.manifest)
        descriptor = os.open(args.device, os.O_WRONLY | os.O_CLOEXEC)
        try:
            for segment, frame in enumerate(frames):
                written = os.write(descriptor, frame)
                if written != len(frame):
                    raise OSError(f"segment {segment} incomplete write: {written}/{len(frame)}")
        finally:
            os.close(descriptor)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print("four firmware segments staged and closed; GPU visibility remains unverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
