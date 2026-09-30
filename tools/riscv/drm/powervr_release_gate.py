#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Build an offline initramfs that stages and releases PowerVR META once."""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "tools" / "riscv"))

from tools.riscv.drm.powervr_dma_stage import checked_frames
from tools.riscv.make_qemu_uboot_initramfs import InitramfsEntry, make_newc_archive


INIT_SOURCE = Path(__file__).with_name("powervr_release_gate_init.c")
SEGMENT_NAMES = ("code", "data", "coremem_code", "coremem_data")


def _compile_init(output: Path) -> None:
    subprocess.run(
        [
            "riscv64-linux-gnu-gcc",
            "-nostdlib",
            "-static",
            "-fno-builtin",
            "-fno-pie",
            "-no-pie",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wl,-e,_start",
            "-o",
            str(output),
            str(INIT_SOURCE),
        ],
        check=True,
    )


def build_initramfs(output: Path, segments_dir: Path, manifest: Path) -> None:
    # checked_frames performs the same digest/size/config validation as the
    # normal userspace staging tool. The frame payloads are not copied into
    # the archive; the C init reads the raw segment files and emits one frame
    # per write, preserving the kernel's write_at transaction boundary.
    frames = checked_frames(segments_dir, manifest, require_boot_config=True)
    raw_segments = [frame[12:] for frame in frames]
    with tempfile.TemporaryDirectory(prefix="powervr-release-init-") as directory:
        init_path = Path(directory) / "init"
        _compile_init(init_path)
        init_elf = init_path.read_bytes()
    archive = make_newc_archive(
        init_elf,
        extra_entries=tuple(
            InitramfsEntry(f"pvr/{name}.bin", payload, 0o100644)
            for name, payload in zip(SEGMENT_NAMES, raw_segments, strict=True)
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(gzip.compress(archive, mtime=0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segments-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_initramfs(args.output, args.segments_dir, args.manifest)
    print(f"built offline PowerVR release initramfs: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
