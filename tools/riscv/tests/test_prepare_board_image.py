#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""The kernel has to be inside the rootfs image U-Boot loads it from."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.riscv.board.prepare_board_image import inject, sha256_of

REPOSITORY_ROOT = Path(__file__).parents[3]
SCRIPT = REPOSITORY_ROOT / "tools/riscv/board/prepare_board_image.py"


def make_image(directory: Path, size_mb: int = 8) -> Path:
    image = directory / "root.ext2"
    image.write_bytes(b"")
    subprocess.run(
        ["mke2fs", "-q", "-t", "ext2", "-F", str(image), f"{size_mb}M"],
        check=True,
        capture_output=True,
    )
    return image


class PrepareBoardImageTests(unittest.TestCase):
    def test_the_kernel_lands_in_the_image_and_reads_back_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = make_image(directory)
            kernel = directory / "kernel.img"
            kernel.write_bytes(b"\x7fELF" + b"kernel payload" * 4096)

            digest = inject(image, kernel, "/root/kernel-rel.img")

            self.assertEqual(digest, sha256_of(kernel))
            self.assertGreater(image.stat().st_size, 0)

    def test_a_second_preparation_replaces_rather_than_appends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = make_image(directory)
            kernel = directory / "kernel.img"
            kernel.write_bytes(b"first" * 1000)
            inject(image, kernel, "/root/kernel-rel.img")

            kernel.write_bytes(b"second" * 1000)
            digest = inject(image, kernel, "/root/kernel-rel.img")

            self.assertEqual(digest, sha256_of(kernel))

    def test_a_missing_kernel_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = make_image(directory)
            with self.assertRaisesRegex(SystemExit, "no such kernel"):
                inject(image, directory / "absent.img", "/root/kernel-rel.img")


if __name__ == "__main__":
    unittest.main()
