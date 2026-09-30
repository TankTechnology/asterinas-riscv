#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the offline PowerVR release gate on Megrez and verify recovery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import zlib

REPO_ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "tools" / "riscv"))

from tools.riscv.debian.rootfs.gate_runtime import SerialConsole
from tools.riscv.megrez_board_session import (
    BoardSession,
    boot_loaded_artifacts,
    open_serial,
    parse_args,
    validate_recovery_epoch,
)
from tools.riscv.megrez_debug_board import _lock_serial


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
    parser.add_argument("--initrd", required=True)
    parser.add_argument("--dtb", required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--boot-timeout", type=float, default=180.0)
    args = parser.parse_args()

    directory = args.artifact_dir.resolve(strict=True)
    names = [args.kernel, args.kernel_lzma, args.initrd, args.dtb]
    paths = {name: (directory / name).resolve(strict=True) for name in names}
    if any(path.parent != directory for path in paths.values()):
        parser.error("artifacts must be direct children of --artifact-dir")
    expected = {name: crc32(path) for name, path in paths.items()}
    bootargs = (
        "console=ttyS0 loglevel=off init=/init asterinas.reboot_after=120 "
        "asterinas.powervr=1 asterinas.powervr_dma_stage=1 "
        "asterinas.powervr_boot_config_preflight=1"
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
        serial = SerialConsole(fd, max_bytes=256 * 1024, tx_delay=0.005)
        serial.send(b"\n", time.monotonic() + 5)
        serial.send(b"sync; reboot -f\n", time.monotonic() + 20)
        session = BoardSession.from_fd(
            fd,
            str(args.log),
            confirm=False,
            final_marker="PVR_RELEASE_PASS",
        )
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
