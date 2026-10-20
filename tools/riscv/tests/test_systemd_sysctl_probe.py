# SPDX-License-Identifier: MPL-2.0

import subprocess
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.dev_overlay import load_overlay_spec


ROOTFS = Path(__file__).resolve().parents[1] / "debian" / "rootfs"


class ProbeArtifactTests(unittest.TestCase):
    def test_probe_script_syntax(self):
        script = ROOTFS / "sysctl_probe.sh"
        self.assertTrue(script.is_file())
        for shell in ("sh", "bash"):
            completed = subprocess.run(
                [shell, "-n", str(script)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_probe_script_contract(self):
        text = (ROOTFS / "sysctl_probe.sh").read_text(encoding="utf-8")
        for required in (
            "SYSCTL_PROBE begin",
            "SYSCTL_PROBE end",
            "timeout 15",
            "/proc/sys/kernel/pid_max",
            "/proc/sys/kernel/core_pattern",
            "/usr/lib/systemd/systemd-sysctl",
            "asterinas_probe_missing",
            "readback_pid_max",
            "readback_core_pattern",
        ):
            self.assertIn(required, text)

    def test_instrumented_unit_contract(self):
        text = (ROOTFS / "systemd-sysctl-probe.service").read_text(
            encoding="utf-8"
        )
        for required in (
            "ConditionPathIsReadWrite=/proc/sys/net/",
            "Before=sysinit.target shutdown.target",
            "ExecStart=/usr/lib/asterinas/sysctl-probe.sh",
            "StandardOutput=console",
            "StandardError=console",
            "TimeoutSec=90s",
            "ImportCredential=sysctl.*",
        ):
            self.assertIn(required, text)

    def test_overlay_spec_contract(self):
        spec = load_overlay_spec(
            ROOTFS / "systemd-sysctl-probe-overlay.json",
            expected_profile="desktop-m4-firefox",
        )
        self.assertEqual(len(spec.files), 2)
        by_destination = {entry.destination: entry for entry in spec.files}
        probe = by_destination["/usr/lib/asterinas/sysctl-probe.sh"]
        self.assertTrue(probe.create)
        self.assertEqual(probe.mode, 0o755)
        unit = by_destination[
            "/usr/lib/systemd/system/systemd-sysctl.service"
        ]
        self.assertFalse(unit.create)
        self.assertEqual(unit.mode, 0o644)


if __name__ == "__main__":
    unittest.main()
