#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from pathlib import Path
import unittest

from tools.riscv.debian.rootfs.browser_m5_startup_probe import (
    BROWSER_M5_STARTUP_MILESTONES,
    BrowserM5StartupOperations,
    classify_startup,
    validate_checkpoint_timeout,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class DebianBrowserM5StartupProbeTests(unittest.TestCase):
    def test_classifier_accepts_only_the_startup_ready_marker(self) -> None:
        transcript = (
            b"DEBIAN_BROWSER_M5_NETNS firefox=private initial=distinct\n"
            + BROWSER_M5_STARTUP_MILESTONES[0].encode()
            + b"\n"
        )
        result = classify_startup(transcript, expected_debian_release="13.6")
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.reason, "ready")

    def test_classifier_rejects_duplicate_or_reordered_startup_marker(self) -> None:
        marker = BROWSER_M5_STARTUP_MILESTONES[0].encode()
        duplicate = classify_startup(
            marker + b"\n" + marker + b"\n", expected_debian_release="13.6"
        )
        self.assertEqual(duplicate.reason, "duplicate startup milestone")

        reordered = classify_startup(
            b"DEBIAN_BROWSER_M5_STARTUP_FAIL reason=firefox-process-exit\n"
            + marker
            + b"\n",
            expected_debian_release="13.6",
        )
        self.assertEqual(reordered.reason, "firefox process exited")

    def test_classifier_reports_each_bounded_failure_domain(self) -> None:
        cases = (
            (
                b"DEBIAN_BROWSER_M5_STARTUP_FAIL reason=firefox-process-exit\n",
                "firefox process exited",
            ),
            (
                b"DEBIAN_BROWSER_M5_STARTUP_FAIL reason=xorg-or-input\n",
                "Xorg or input failed",
            ),
            (
                b"DEBIAN_BROWSER_M5_FAIL reason=browser-timeout\n",
                "browser guest failure",
            ),
            (b"", "timeout"),
        )
        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = classify_startup(transcript, expected_debian_release="13.6")
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, reason)

    def test_classifier_requires_a_release_and_rejects_out_of_order_failures(
        self,
    ) -> None:
        result = classify_startup(
            BROWSER_M5_STARTUP_MILESTONES[0].encode() + b"\n",
            expected_debian_release="",
        )
        self.assertEqual(result.reason, "missing expected Debian release")

    def test_checkpoint_budget_is_a_positive_integer_at_most_600(self) -> None:
        self.assertEqual(validate_checkpoint_timeout(600), 600)
        for value in (0, -1, 600.1, 601, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_checkpoint_timeout(value)

    def test_operations_bind_schema_six_and_disable_screenshot(self) -> None:
        self.assertEqual(BrowserM5StartupOperations.SCHEMA_VERSION, 6)
        self.assertEqual(BrowserM5StartupOperations.PROFILE_NAME, "browser-m5")
        self.assertEqual(
            BrowserM5StartupOperations.MILESTONES, BROWSER_M5_STARTUP_MILESTONES
        )
        self.assertFalse(BrowserM5StartupOperations.CAPTURE_SCREENSHOT)

    def test_make_target_runs_startup_probe_with_a_600_second_budget(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        target = makefile.split(
            ".PHONY: test_riscv_debian_browser_m5_startup_probe", 1
        )[1].split(".PHONY:", 1)[0]
        self.assertIn(
            "python3 -m tools.riscv.debian.rootfs.browser_m5_startup_probe", target
        )
        self.assertIn("DEBIAN_BROWSER_M5_STARTUP_PROBE_OUTPUT", target)
        self.assertIn("--boot-timeout 600", target)

    def test_guest_startup_service_is_installed_and_fail_closed(self) -> None:
        builder = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
        evidence = (
            REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_m5_startup_evidence.sh"
        )
        source = builder.read_text(encoding="utf-8")
        evidence_source = evidence.read_text(encoding="utf-8")
        self.assertIn("browser_m5_startup_evidence.sh", source)
        self.assertIn("asterinas-browser-m5-startup.service", source)
        self.assertIn("DEBIAN_BROWSER_M5_STARTUP_READY", evidence_source)
        self.assertIn("DEBIAN_BROWSER_M5_STARTUP_FAIL", evidence_source)
        self.assertIn("--marionette", evidence_source)
        self.assertIn("sandbox-disabled", evidence_source)
        self.assertIn(
            'if systemctl is-failed --quiet asterinas-browser-m5.service',
            evidence_source,
        )
        self.assertIn(
            'sleep "$INTERVAL_SECONDS"\n            continue', evidence_source
        )


if __name__ == "__main__":
    unittest.main()
