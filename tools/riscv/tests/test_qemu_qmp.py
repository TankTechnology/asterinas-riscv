#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the bounded QMP screendump and tablet helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.riscv import qemu_qmp


class TabletEventTests(unittest.TestCase):
    def test_movements_start_at_origin_then_reach_the_target(self) -> None:
        groups = qemu_qmp._tablet_movements(100, 200)

        self.assertEqual(len(groups), 2)
        self.assertEqual(
            groups[0],
            [
                {"type": "abs", "data": {"axis": "x", "value": 0}},
                {"type": "abs", "data": {"axis": "y", "value": 0}},
            ],
        )
        self.assertEqual(
            groups[1],
            [
                {"type": "abs", "data": {"axis": "x", "value": 100}},
                {"type": "abs", "data": {"axis": "y", "value": 200}},
            ],
        )

    def test_coordinates_are_bounded_integers(self) -> None:
        for value in (-1, qemu_qmp.ABSOLUTE_AXIS_MAX + 1, True, 1.0):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "coordinates"):
                qemu_qmp._tablet_movements(value, 0)

    def test_left_click_presses_and_releases(self) -> None:
        down, up = qemu_qmp._left_click()

        self.assertTrue(down[0]["data"]["down"])
        self.assertFalse(up[0]["data"]["down"])
        self.assertEqual(down[0]["data"]["button"], "left")


class PathValidationTests(unittest.TestCase):
    def test_private_root_accepts_an_absent_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            socket_path = root / "qmp.sock"
            output = root / "screen.ppm"

            qemu_qmp._validate_paths(
                socket_path,
                output,
                root,
                validate_socket=False,
            )

    def test_non_private_root_and_outside_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "0700"):
                qemu_qmp._validate_paths(
                    root / "qmp.sock",
                    root / "out.ppm",
                    root,
                    validate_socket=False,
                )

            root.chmod(0o700)
            with self.assertRaisesRegex(ValueError, "strictly below"):
                qemu_qmp._validate_paths(
                    root / "qmp.sock",
                    root.parent / "out.ppm",
                    root,
                    validate_socket=False,
                )

    def test_symlink_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            target = root / "target.ppm"
            target.write_bytes(b"target")
            output = root / "screen.ppm"
            output.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "symlink|regular file"):
                qemu_qmp._validate_paths(
                    root / "qmp.sock",
                    output,
                    root,
                    validate_socket=False,
                )

    def test_relative_and_parent_traversal_paths_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            qemu_qmp._require_path(Path("relative"), "path")
        with self.assertRaisesRegex(ValueError, "parent traversal"):
            qemu_qmp._require_path(Path("/tmp/../etc/passwd"), "path")


if __name__ == "__main__":
    unittest.main()
