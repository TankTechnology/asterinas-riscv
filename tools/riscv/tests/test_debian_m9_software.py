#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m9_software_gate import (
    DESKTOP_M8_READY_MARKER,
    DESKTOP_M9_SOFTWARE_READY_MARKER,
    DesktopM9SoftwareOperations,
    classify_desktop_m9_software,
)
from tools.riscv.debian.rootfs.profiles import get_profile


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m9_software_evidence.sh"
)
ROOTFS_BUILDER = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
MAKEFILE = REPOSITORY_ROOT / "Makefile"


def _software_transcript() -> bytes:
    markers = (
        *DESKTOP_M5_QEMU_MILESTONES,
        *DESKTOP_M4_MILESTONES,
        DESKTOP_M8_READY_MARKER,
        DESKTOP_M9_SOFTWARE_READY_MARKER,
    )
    return ("\n".join(markers) + "\n").encode()


class DebianDesktopM9SoftwareContractTests(unittest.TestCase):
    def test_profile_adds_vim_and_ffmpeg_without_fake_ffprobe_package(self) -> None:
        profile = get_profile("desktop-m9-software")

        self.assertEqual(profile.schema_version, 5)
        self.assertEqual(profile.root_label, "ASTER_DEBIANM9")
        self.assertEqual(profile.root_uuid, "6f7af5d0-3f2f-5a32-9f7e-c0aa3c8c2e91")
        self.assertIn("netsurf-gtk", profile.requested_packages)
        self.assertIn("vim", profile.requested_packages)
        self.assertIn("ffmpeg", profile.requested_packages)
        self.assertNotIn("ffprobe", profile.requested_packages)
        self.assertIn("vim", profile.identity_packages)
        self.assertIn("ffmpeg", profile.identity_packages)

    def test_classifier_accepts_ordered_m8_and_software_evidence(self) -> None:
        result = classify_desktop_m9_software(
            _software_transcript(), expected_debian_release="13.6"
        )

        self.assertTrue(result.passed, result.reason)

    def test_classifier_rejects_missing_duplicate_reordered_and_failure(self) -> None:
        valid = _software_transcript()
        software = (DESKTOP_M9_SOFTWARE_READY_MARKER + "\n").encode()
        cases = (
            valid.replace(software, software * 2),
            valid.replace(software, b""),
            valid + b"DEBIAN_DESKTOP_M9_FAIL reason=ffmpeg-timeout\n",
        )

        for transcript in cases:
            with self.subTest(transcript=transcript[-180:]):
                result = classify_desktop_m9_software(
                    transcript, expected_debian_release="13.6"
                )
                self.assertFalse(result.passed)

    def test_classifier_does_not_require_m8_quality_marker(self) -> None:
        transcript = _software_transcript().replace(
            (DESKTOP_M8_READY_MARKER + "\n").encode(), b""
        )
        result = classify_desktop_m9_software(
            transcript, expected_debian_release="13.6"
        )
        self.assertTrue(result.passed, result.reason)

    def test_builder_and_makefile_expose_m9_contract(self) -> None:
        builder = ROOTFS_BUILDER.read_text(encoding="utf-8")
        makefile = MAKEFILE.read_text(encoding="utf-8")

        self.assertIn("desktop-m9-software", builder)
        self.assertIn("desktop_m9_software_evidence.sh", builder)
        self.assertIn(
            "After=asterinas-desktop-m7-baidu.service", builder
        )
        self.assertIn(
            "asterinas-desktop-m8-browser-quality.service", builder
        )
        self.assertIn("rm -f", builder)
        self.assertIn(
            "ASTERINAS_DESKTOP_M9_COMMAND_TIMEOUT_SECONDS=120", builder
        )
        self.assertIn("ASTERINAS_DESKTOP_M9_WORK_DIRECTORY=/var/tmp", builder)
        self.assertIn("test_riscv_debian_desktop_m9_software_gate", makefile)

    def test_gate_stops_immediately_on_stage1_failure(self) -> None:
        self.assertIn(
            b"DEBIAN_ROOTFS_FAIL reason=",
            DesktopM9SoftwareOperations.ADDITIONAL_FAILURE_MARKERS,
        )


class DebianDesktopM9SoftwareGuestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def _install_tool(self, name: str, contents: str) -> None:
        path = self.directory / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def test_guest_smoke_emits_exact_pass_marker(self) -> None:
        script = EVIDENCE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("-f rawvideo", script)
        self.assertIn("-threads 1", script)
        self.assertIn("${ASTERINAS_DESKTOP_M9_WORK_DIRECTORY:-/var/tmp}", script)
        self.assertNotIn("-f lavfi", script)
        console = self.directory / "console"
        self._install_tool(
            "vim",
            """#!/bin/sh
set -eu
printf 'ASTERINAS_VIM_PASS\\n' >"$ASTERINAS_DESKTOP_M9_VIM_OUTPUT"
""",
        )
        self._install_tool(
            "ffmpeg",
            """#!/bin/sh
set -eu
last=''
for argument in "$@"; do
    last="$argument"
done
printf 'fake-png' >"$last"
""",
        )
        self._install_tool(
            "ffprobe",
            """#!/bin/sh
set -eu
printf '16,16\\n'
""",
        )

        environment = os.environ.copy()
        environment.update(
            PATH=f"{self.directory / 'bin'}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_M9_CONSOLE=str(console),
            ASTERINAS_DESKTOP_M9_TIMEOUT_SECONDS="5",
            ASTERINAS_DESKTOP_M9_COMMAND_TIMEOUT_SECONDS="1",
            ASTERINAS_DESKTOP_M9_WORK_DIRECTORY=str(self.directory),
            ASTERINAS_DESKTOP_M9_VIM_OUTPUT=str(self.directory / "vim-output"),
        )
        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines(),
            [DESKTOP_M9_SOFTWARE_READY_MARKER],
        )

    def test_guest_smoke_reports_a_bounded_failure(self) -> None:
        console = self.directory / "console"
        self._install_tool("vim", "#!/bin/sh\nexit 42\n")

        environment = os.environ.copy()
        environment.update(
            PATH=f"{self.directory / 'bin'}:/usr/bin:/bin",
            ASTERINAS_DESKTOP_M9_CONSOLE=str(console),
            ASTERINAS_DESKTOP_M9_TIMEOUT_SECONDS="1",
            ASTERINAS_DESKTOP_M9_COMMAND_TIMEOUT_SECONDS="1",
            ASTERINAS_DESKTOP_M9_WORK_DIRECTORY=str(self.directory),
        )
        result = subprocess.run(
            ["/bin/bash", str(EVIDENCE_SCRIPT)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            console.read_text(encoding="utf-8").splitlines(),
            ["DEBIAN_DESKTOP_M9_FAIL reason=vim-failed"],
        )


if __name__ == "__main__":
    unittest.main()
