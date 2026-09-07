#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import unittest
from dataclasses import replace

from tools.riscv.debian.rootfs.debug_console_protocol import (
    MAX_DEBUG_CONSOLE_TRANSCRIPT_BYTES,
    DebugConsoleEvidence,
    DebugConsoleProtocolError,
    classify_debug_console,
    debug_console_commands,
)


NONCE = "0123456789abcdef0123456789abcdef"
PASSING_OUTPUTS = {
    "uid": "0",
    "pid1": "systemd",
    "root": "/dev/mmcblk0p2 ext2",
    "graphical": "active",
    "desktop": "active",
}


def make_transcript(
    *,
    outputs: dict[str, str] | None = None,
    statuses: dict[str, int] | None = None,
) -> str:
    selected_outputs = PASSING_OUTPUTS if outputs is None else outputs
    selected_statuses = {} if statuses is None else statuses
    lines: list[str] = []
    for command in debug_console_commands(NONCE):
        lines.extend(
            (
                command.begin_marker,
                selected_outputs[command.name],
                f"{command.status_prefix}{selected_statuses.get(command.name, 0)}",
                command.end_marker,
            )
        )
    return "\n".join(lines) + "\n"


class DebugConsoleProtocolTests(unittest.TestCase):
    def test_passing_transcript_yields_exact_evidence(self) -> None:
        self.assertEqual(
            classify_debug_console(make_transcript(), NONCE),
            DebugConsoleEvidence(
                uid=0,
                pid1="systemd",
                root_device="/dev/mmcblk0p2",
                root_filesystem="ext2",
                graphical_state="active",
                desktop_state="active",
            ),
        )

    def test_commands_are_five_fixed_read_only_probes(self) -> None:
        commands = debug_console_commands(NONCE)
        self.assertEqual(
            tuple(command.name for command in commands),
            ("uid", "pid1", "root", "graphical", "desktop"),
        )
        self.assertEqual(len({command.payload for command in commands}), 5)
        self.assertTrue(all(NONCE in command.payload for command in commands))

    def assert_output_rejected(self, name: str, value: str) -> None:
        outputs = dict(PASSING_OUTPUTS)
        outputs[name] = value
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(make_transcript(outputs=outputs), NONCE)

    def test_rejects_non_root_uid(self) -> None:
        self.assert_output_rejected("uid", "1000")

    def test_rejects_non_systemd_pid1(self) -> None:
        self.assert_output_rejected("pid1", "bash")

    def test_rejects_wrong_megrez_root_partition(self) -> None:
        self.assert_output_rejected("root", "/dev/mmcblk0p3 ext2")

    def test_accepts_qemu_virtio_root_partition(self) -> None:
        outputs = dict(PASSING_OUTPUTS)
        outputs["root"] = "/dev/vdb ext2"
        evidence = classify_debug_console(make_transcript(outputs=outputs), NONCE)
        self.assertEqual(evidence.root_device, "/dev/vdb")

    def test_rejects_non_ext2_root(self) -> None:
        self.assert_output_rejected("root", "/dev/mmcblk0p2 ext4")

    def test_rejects_inactive_graphical_target(self) -> None:
        self.assert_output_rejected("graphical", "inactive")

    def test_rejects_inactive_desktop_service(self) -> None:
        self.assert_output_rejected("desktop", "failed")

    def test_rejects_nonzero_command_status(self) -> None:
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(
                make_transcript(statuses={"graphical": 3}), NONCE
            )

    def test_rejects_duplicate_marker(self) -> None:
        transcript = make_transcript()
        marker = debug_console_commands(NONCE)[0].begin_marker
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(f"{marker}\n{transcript}", NONCE)

    def test_rejects_reordered_markers(self) -> None:
        commands = debug_console_commands(NONCE)
        first, second = commands[:2]
        transcript = make_transcript().replace(
            first.begin_marker, second.begin_marker, 1
        ).replace(first.end_marker, second.end_marker, 1)
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(transcript, NONCE)

    def test_rejects_stale_nonce_marker(self) -> None:
        stale = "fedcba9876543210fedcba9876543210"
        transcript = make_transcript() + debug_console_commands(stale)[0].begin_marker
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(transcript, NONCE)

    def test_rejects_ansi_or_osc_control_input(self) -> None:
        for sequence in ("\x1b[31m", "\x1b]0;spoofed\x07"):
            with self.subTest(sequence=repr(sequence)):
                with self.assertRaises(DebugConsoleProtocolError):
                    classify_debug_console(sequence + make_transcript(), NONCE)

    def test_rejects_transcript_over_eight_mib(self) -> None:
        oversized = b"x" * (MAX_DEBUG_CONSOLE_TRANSCRIPT_BYTES + 1)
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(oversized, NONCE)

    def test_rejects_invalid_nonce(self) -> None:
        for nonce in ("", "A" * 32, "0" * 31, "0" * 33, "0" * 31 + "\n"):
            with self.subTest(nonce=nonce):
                with self.assertRaises(ValueError):
                    debug_console_commands(nonce)

    def test_rejects_mutated_command_contract(self) -> None:
        command = debug_console_commands(NONCE)[0]
        mutated = replace(command, payload="id")
        self.assertNotEqual(mutated, command)


if __name__ == "__main__":
    unittest.main()
