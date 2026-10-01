#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the offline PowerVR release gate on Megrez and verify recovery."""

from __future__ import annotations

import argparse
import json
import lzma
import os
from pathlib import Path
import re
import sys
import zlib

REPO_ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "tools" / "riscv"))

from tools.riscv.megrez_board_session import (
    BoardSession,
    boot_loaded_artifacts,
    open_serial,
    parse_args,
    validate_recovery_epoch,
)
from tools.riscv.megrez_debug_board import _lock_serial


def validate_boot_kernel(bundle: Path, kernel: Path, compressed: Path) -> None:
    """Reject test/stale artifacts before acquiring or writing the UART."""
    record = bundle.read_text()
    # Read only the exact fields emitted by OSDK, failing closed on missing
    # or duplicate fields. This keeps the runner usable with host Python 3.10
    # without requiring a third-party TOML parser.
    header = record.split("[", 1)[0]
    actions = re.findall(r'^action = "([^"]+)"$', header, re.MULTILINE)
    if actions != ["Run"]:
        raise ValueError("physical boot requires an OSDK Run bundle")
    sections = re.findall(r'^\[aster_bin\]\n(.*?)(?=^\[|\Z)', record, re.MULTILINE | re.DOTALL)
    if len(sections) != 1:
        raise ValueError("bundle lacks an unambiguous kernel record")
    names = re.findall(r'^path = "([^"/]+)"$', sections[0], re.MULTILINE)
    if len(names) != 1 or names[0] in (".", ".."):
        raise ValueError("bundle has an invalid kernel path")
    if not 0 < kernel.stat().st_size <= 64 * 1024 * 1024:
        raise ValueError("kernel image exceeds the physical artifact limit")
    image = kernel.read_bytes()
    if b"[ktest runner]" in image:
        raise ValueError("ktest image is forbidden on the physical release path")
    if (bundle.parent / names[0]).read_bytes() != image:
        raise ValueError("frozen kernel does not match its OSDK Run bundle")
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE, memlimit=128 * 1024 * 1024)
    expanded = decoder.decompress(compressed.read_bytes(), max_length=len(image) + 1)
    if expanded != image or not decoder.eof or decoder.unused_data:
        raise ValueError("compressed kernel does not match the frozen Run image")


class OfflineBoardSession(BoardSession):
    """Keep this gate's DTB offline without changing other board workflows."""

    artifact_directory: Path

    def load_artifact(self, name, filename, address, expected_crc32, **kwargs):
        if name != "dtb":
            raise ValueError("offline release gate forbids MMC artifact loading")
        return self.load_ymodem_artifact(
            name, self.artifact_directory, filename, address, expected_crc32
        )


def crc32(path: Path) -> str:
    value = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value = zlib.crc32(chunk, value)
    return f"{value & 0xFFFF_FFFF:08x}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device")
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--kernel", required=True)
    parser.add_argument("--kernel-lzma", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--reset-recovery-available", action="store_true",
        help="confirm an independent reset path is available if the kernel hard-locks",
    )
    parser.add_argument("--initrd", required=True)
    parser.add_argument("--dtb", required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--boot-timeout", type=float, default=180.0)
    args = parser.parse_args()

    if not args.reset_recovery_available:
        parser.error("META release requires an independent reset recovery path")

    directory = args.artifact_dir.resolve(strict=True)
    names = [args.kernel, args.kernel_lzma, args.initrd, args.dtb]
    paths = {name: (directory / name).resolve(strict=True) for name in names}
    if any(path.parent != directory for path in paths.values()):
        parser.error("artifacts must be direct children of --artifact-dir")
    try:
        validate_boot_kernel(args.bundle, paths[args.kernel], paths[args.kernel_lzma])
    except (ValueError, OSError, lzma.LZMAError) as error:
        parser.error(str(error))
    expected = {name: crc32(path) for name, path in paths.items()}
    bootargs = (
        "console=ttyS0 loglevel=off init=/init asterinas.reboot_after=90 "
        "asterinas.powervr=1 asterinas.powervr_dma_stage=1 "
        "asterinas.powervr_boot_config_preflight=1 asterinas.powervr_release=1"
    )
    forwarded = parse_args(
        [
            args.device,
            "--booti",
            args.kernel_lzma,
            "--initrd",
            args.initrd,
            "--dtb",
            args.dtb,
            "--bootargs",
            bootargs,
            "--load-transport",
            "ymodem",
            "--ymodem-directory",
            str(directory),
            "--booti-compressed-crc32",
            expected[args.kernel_lzma],
            "--booti-uncompressed-size",
            str(paths[args.kernel].stat().st_size),
            "--expected-crc32",
            f"booti={expected[args.kernel]},dtb={expected[args.dtb]},initrd={expected[args.initrd]}",
            "--final-profile",
            "drm-firmware",
            "--yes",
            "--uboot-timeout",
            "180",
            "--milestone-timeout",
            str(args.boot_timeout),
        ]
    )

    fd = open_serial(args.device)
    _lock_serial(fd)
    session = None
    try:
        session = OfflineBoardSession.from_fd(
            fd,
            str(args.log),
            confirm=False,
            final_marker="PVR_RELEASE_PASS",
        )
        session.artifact_directory = directory
        # This runner never sends an unauthenticated reboot command. The
        # operator or an authenticated RockOS SSH wrapper must first return
        # the board to U-Boot; here we only interrupt an autoboot countdown.
        session.interrupt()
        session.wait_for_uboot_prompt(timeout=180)
        boot_loaded_artifacts(session, forwarded)
        session.wait_for("PVR_RELEASE_PASS", timeout=args.boot_timeout)
        recovery = session.wait_for_uboot_prompt(timeout=150)
        validate_recovery_epoch(recovery)
        print(json.dumps({"release": "pass", "recovery": "pass"}))
        return 0
    except Exception as error:
        print(f"PowerVR release gate failed: {error}", file=sys.stderr)
        return 2
    finally:
        if session is not None:
            session.log.close()
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
