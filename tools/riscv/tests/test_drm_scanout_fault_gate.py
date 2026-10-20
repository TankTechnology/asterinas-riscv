#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from drm.scanout_fault_gate import (  # noqa: E402
    FAULT_PROFILES,
    classify_fault_transcript,
)
from qemu_uboot_profiles import profile_by_name  # noqa: E402


class DrmScanoutFaultClassifierTests(unittest.TestCase):
    def test_accepts_each_stage_and_rejects_ready(self) -> None:
        for fault in ("setcrtc", "transfer", "flush"):
            with self.subTest(fault=fault):
                outer_stage = "setcrtc" if fault != "setcrtc" else fault
                transcript = (
                    f"DRM_SCANOUT_FAIL stage={outer_stage} errno=5\n"
                    f"DRM_SCANOUT_FAULT stage={fault}\n"
                ).encode()
                result = classify_fault_transcript(
                    transcript, requested_fault=fault, expected_stage=fault
                )
                self.assertTrue(result.passed, result.reason)
                self.assertEqual(result.reported_stage, fault)
                self.assertFalse(
                    classify_fault_transcript(
                        transcript + b"ASTERINAS_DRM_SCANOUT_R1_READY\n",
                        requested_fault=fault,
                        expected_stage=fault,
                    ).passed
                )

    def test_accepts_qemu_crlf_and_rejects_wrong_or_duplicate_stage(self) -> None:
        transcript = b"DRM_SCANOUT_FAIL stage=transfer errno=5\r\nDRM_SCANOUT_FAULT stage=transfer\r\n"
        self.assertTrue(
            classify_fault_transcript(
                transcript, requested_fault="transfer", expected_stage="transfer"
            ).passed
        )
        wrong = classify_fault_transcript(
            transcript, requested_fault="flush", expected_stage="flush"
        )
        self.assertFalse(wrong.passed)
        duplicate = transcript + b"DRM_SCANOUT_FAULT stage=transfer\n"
        self.assertFalse(
            classify_fault_transcript(
                duplicate, requested_fault="transfer", expected_stage="transfer"
            ).passed
        )

    def test_profiles_are_registered_with_recovery(self) -> None:
        for fault, profile_name in FAULT_PROFILES.items():
            with self.subTest(fault=fault):
                profile = profile_by_name(profile_name)
                self.assertIsNotNone(profile.validation.recovery)
                assert profile.validation.recovery is not None
                self.assertIn(
                    f"asterinas.virtio_gpu_2d_fault={fault}",
                    profile.validation.bootargs,
                )
                self.assertEqual(
                    profile.validation.completion_line,
                    f"DRM_SCANOUT_FAULT stage={fault}".encode(),
                )
                self.assertEqual(profile.validation.post_terminal_timeout, 0.0)

    def test_scanout_probe_compiles_with_stage_fault_receipt(self) -> None:
        source = TOOLS / "drm/scanout_gate_init.c"
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "scanout-self-test"
            result = subprocess.run(
                [
                    "cc",
                    "-std=c11",
                    "-O2",
                    "-static",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-DDRM_SCANOUT_GATE_SELF_TEST",
                    str(source),
                    "-o",
                    str(binary),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                subprocess.run(
                    [binary], capture_output=True, text=True, check=False
                ).stdout,
                "DRM_SCANOUT_SELF_TEST PASS layout\n",
            )


if __name__ == "__main__":
    unittest.main()
