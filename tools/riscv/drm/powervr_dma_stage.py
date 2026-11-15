#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Stage checked PowerVR META segments into one root-only DMA control session."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct

SEGMENTS = (("code", 52_064), ("data", 18_432), ("coremem_code", 73_312), ("coremem_data", 9_984))
EXPECTED_FIRMWARE_SHA256 = "25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b"
EXPECTED_BOOT_CONFIG_SHA256 = "2c70442a16a343dbae1312d3d1f54fedf773a26d544ff4702c58cb8fcd88d4d5"
BOOT_CONFIG_FIELDS = {
    "firmware_vaddrs": [0xE1C0000000, 0xE1C000E000, 0xE1C0014000, 0xE1C0027000],
    "boot_config_offset": 512, "boot_config_bytes": 296,
    "boot_config_pairs": 34, "boot_config_ldr_writes": 17,
    "boot_config_sha256": EXPECTED_BOOT_CONFIG_SHA256,
    "meta_threads": 2, "meta_dma": True, "slc_vivt": True,
}
DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "docs/porting/evidence/2026-09-30-megrez-powervr-fw-layout/ldr-scan.json"
)
STATUS_SIZE = 48
STATUS_FIELDS = (
    "meta_release_attempted", "firmware_started", "started_timestamp",
    "firmware_faults", "hwr_state", "hwr_count", "compatibility_updated",
    "ddk_version", "ddk_build", "build_options", "connection_fw_state",
)


def decode_status(payload: bytes) -> dict[str, int]:
    if len(payload) != STATUS_SIZE or payload[:4] != b"PVS1":
        raise ValueError("incompatible PowerVR status frame")
    return dict(zip(STATUS_FIELDS, struct.unpack("<11I", payload[4:])))


def require_stage_opt_in(cmdline: str, *, boot_config_check: bool = False) -> None:
    parameters = set(cmdline.split())
    required = ["asterinas.powervr=1", "asterinas.powervr_dma_stage=1"]
    if boot_config_check:
        required.append("asterinas.powervr_boot_config_preflight=1")
    for flag in required:
        if flag not in parameters:
            raise ValueError(f"PowerVR DMA staging requires {flag}")


def checked_frames(segments_dir: Path, manifest: Path, *, require_boot_config: bool = False) -> list[bytes]:
    record = json.loads(manifest.read_text())
    if record.get("processor") != "META" or record.get("firmware_sha256") != EXPECTED_FIRMWARE_SHA256:
        raise ValueError("firmware manifest identity mismatch")
    if require_boot_config and any(record.get(key) != value for key, value in BOOT_CONFIG_FIELDS.items()):
        raise ValueError("selected META boot configuration manifest mismatch")
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
    if require_boot_config:
        config = frames[0][12 + 512:12 + 808]
        if hashlib.sha256(config).hexdigest() != EXPECTED_BOOT_CONFIG_SHA256:
            raise ValueError("selected META boot configuration bytes mismatch")
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--device", type=Path, default=Path("/dev/powervr-control"))
    parser.add_argument("--status", action="store_true", help="read diagnostic firmware state before closing")
    parser.add_argument("--check-boot-config", action="store_true", help="require the prepared META configuration and selected kernel validation flag")
    args = parser.parse_args()
    try:
        require_stage_opt_in(Path("/proc/cmdline").read_text(), boot_config_check=args.check_boot_config)
        frames = checked_frames(args.segments_dir, args.manifest, require_boot_config=args.check_boot_config)
        descriptor = os.open(args.device, (os.O_RDWR if args.status else os.O_WRONLY) | os.O_CLOEXEC)
        try:
            for segment, frame in enumerate(frames):
                written = os.write(descriptor, frame)
                if written != len(frame):
                    raise OSError(f"segment {segment} incomplete write: {written}/{len(frame)}")
            if args.status:
                print(json.dumps({
                    "diagnostic_only": True,
                    "firmware_status": decode_status(os.read(descriptor, STATUS_SIZE)),
                }, sort_keys=True))
        finally:
            os.close(descriptor)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print("four firmware segments staged and closed; GPU visibility remains unverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
