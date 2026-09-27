#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Focused host tests for the ext2 QEMU diagnostic gate."""

import subprocess
import unittest
from unittest import mock

from tools.riscv import ext2_durability_gate as gate


class KillGuestTests(unittest.TestCase):
    def test_kills_guest_using_blkdebug_drive(self) -> None:
        case = gate.REPO / ".local-test" / "flush-eio-fixture"
        image = gate.guest_path(case / "ext2.img")
        config = gate.guest_path(case / "blkdebug.conf")
        process_list = subprocess.CompletedProcess(
            ["docker", "exec"],
            0,
            stdout=(
                f"123 qemu-system-riscv64 -drive "
                f"if=none,format=raw,id=x0,file=blkdebug:{config}:{image}\n"
            ),
        )
        with (
            mock.patch.object(gate, "docker_container", return_value="dev-container"),
            mock.patch.object(gate, "command", return_value=process_list) as command,
        ):
            gate.kill_guest(case)

        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args.args[0][-2:], ["-KILL", "123"])


if __name__ == "__main__":
    unittest.main()
