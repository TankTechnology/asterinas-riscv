#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""The boot-state marker that reports a boot which followed a crash."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOTFS = Path(__file__).parents[1] / "debian/rootfs"
SCRIPT = ROOTFS / "asterinas_boot_state.sh"


def run(state_dir: Path, action: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "ASTERINAS_BOOT_STATE_DIR": str(state_dir)}
    return subprocess.run(
        ["/bin/sh", str(SCRIPT), action],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


class BootStateTests(unittest.TestCase):
    def test_first_boot_is_quiet_and_leaves_the_marker_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            result = run(state, "start")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("ASTERINAS_UNCLEAN_SHUTDOWN", result.stdout)
            self.assertEqual((state / "boot-state").read_text().strip(), "running")

    def test_a_clean_shutdown_keeps_the_next_boot_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            run(state, "start")
            stop = run(state, "stop")
            self.assertEqual(stop.returncode, 0, stop.stderr)
            self.assertEqual((state / "boot-state").read_text().strip(), "clean")

            result = run(state, "start")
            self.assertNotIn("ASTERINAS_UNCLEAN_SHUTDOWN", result.stdout)

    def test_a_crash_is_reported_on_the_next_boot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            run(state, "start")  # leaves `running` behind, as a crash would

            result = run(state, "start")

            self.assertIn(
                "ASTERINAS_UNCLEAN_SHUTDOWN previous_state=running", result.stdout
            )

    def test_the_unit_keeps_default_dependencies(self) -> None:
        # Units with DefaultDependencies=no that hang off basic.target never ran
        # on this image; this one must keep the defaults.
        unit = (ROOTFS / "asterinas-boot-state.service").read_text()
        directives = [
            line
            for line in unit.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(
            any(line.startswith("DefaultDependencies=") for line in directives)
        )
        self.assertIn("WantedBy=basic.target", unit)
        self.assertIn("ExecStop=/bin/sh /usr/lib/asterinas/boot-state stop", unit)


if __name__ == "__main__":
    unittest.main()
