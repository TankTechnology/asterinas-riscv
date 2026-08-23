#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from tools.riscv.debian import input_gate as gate


class InputGateContractTests(unittest.TestCase):
    def test_qemu_argv_uses_smp4_and_two_distinct_input_devices(self) -> None:
        argv = gate.qemu_argv(
            Path("/u-boot"),
            Path("/boot.ext4"),
            Path("/tmp/gate-monitor.sock"),
            4,
        )

        self.assertEqual(argv[argv.index("-machine") + 1], "virt")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertIn("virtio-tablet-device", argv)
        self.assertIn("virtio-keyboard-device", argv)
        self.assertLess(
            argv.index("virtio-tablet-device"),
            argv.index("virtio-keyboard-device"),
        )
        self.assertIn(
            "if=none,format=raw,file=/boot.ext4,id=bootdisk",
            argv,
        )
        self.assertIn("virtio-blk-device,drive=bootdisk", argv)
        self.assertEqual(
            argv[argv.index("-monitor") + 1],
            "unix:/tmp/gate-monitor.sock,server=on,wait=off",
        )
        self.assertEqual(argv[argv.index("-serial") + 1], "stdio")
        self.assertEqual(argv[argv.index("-nic") + 1], "none")

    def test_qemu_argv_rejects_non_positive_or_non_integer_smp(self) -> None:
        paths = (
            Path("/u-boot"),
            Path("/boot.ext4"),
            Path("/tmp/gate-monitor.sock"),
        )

        for invalid_smp in (0, -1, True, False, 1.0, "4"):
            with self.subTest(smp=invalid_smp):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    gate.qemu_argv(*paths, invalid_smp)

    def test_injected_sequence_covers_normal_modifier_and_editing_keys(self) -> None:
        self.assertEqual(
            gate.KEY_SEQUENCE,
            ("a", "shift-b", "backspace", "ctrl-c"),
        )

    def test_classification_requires_ready_and_pass_without_panic(self) -> None:
        transcript = gate.READY_MARKER + b"\n" + gate.PASS_MARKER + b"\n"

        self.assertTrue(gate.classify_transcript(transcript).passed)
        self.assertFalse(gate.classify_transcript(gate.PASS_MARKER).passed)
        self.assertFalse(
            gate.classify_transcript(transcript + b"Kernel panic").passed
        )

    def test_classification_reports_each_panic_marker(self) -> None:
        transcript = (
            gate.READY_MARKER
            + b"\n"
            + gate.PASS_MARKER
            + b"\n"
            + b"\n".join(gate.PANIC_MARKERS)
        )

        result = gate.classify_transcript(transcript)

        self.assertTrue(result.ready)
        self.assertTrue(result.complete)
        self.assertEqual(
            result.panics,
            tuple(marker.decode() for marker in gate.PANIC_MARKERS),
        )
        self.assertFalse(result.passed)

    def test_gate_result_is_frozen(self) -> None:
        result = gate.GateResult(ready=True, complete=True, panics=())

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.ready = False


if __name__ == "__main__":
    unittest.main()
