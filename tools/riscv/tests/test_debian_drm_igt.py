#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

import unittest

from tools.riscv.debian.rootfs.desktop_drm_igt_gate import (
    IGT_BOOTARGS,
    IGT_REQUIRED_PASS,
    classify_drm_igt,
)
from tools.riscv.debian.rootfs.desktop_drm_gate import DESKTOP_DRM_BOOTARGS


def _transcript(results: dict[str, str], done: str | None = "pass") -> bytes:
    lines = ["ASTERINAS_IGT_BEGIN tests=36"]
    for name, status in results.items():
        rc = {"PASS": 0, "SKIP": 77, "FAIL": 1}[status]
        lines.append(f"ASTERINAS_IGT_RESULT test={name} rc={rc} status={status}")
    if done is not None:
        counts = {"PASS": 0, "SKIP": 0, "FAIL": 0}
        for status in results.values():
            counts[status] += 1
        lines.append(
            f"ASTERINAS_IGT_DONE pass={counts['PASS']} "
            f"skip={counts['SKIP']} fail={counts['FAIL']}"
        )
    return "\n".join(lines).encode()


class DebianDrmIgtGateTests(unittest.TestCase):
    def test_bootargs_enable_igt_mode(self) -> None:
        self.assertTrue(IGT_BOOTARGS.startswith(DESKTOP_DRM_BOOTARGS.split(" -- ")[0]))
        self.assertIn("systemd.unit=asterinas-igt.target", IGT_BOOTARGS)
        self.assertTrue(IGT_BOOTARGS.endswith("-- --root-init=systemd"))

    def test_classifier_accepts_full_pass_with_skips(self) -> None:
        results = {name: "PASS" for name in IGT_REQUIRED_PASS}
        results["kms_vblank"] = "SKIP"
        outcome = classify_drm_igt(_transcript(results), expected_debian_release="13.6")
        self.assertTrue(outcome.passed)

    def test_classifier_rejects_failures(self) -> None:
        results = {name: "PASS" for name in IGT_REQUIRED_PASS}
        results["kms_flip"] = "FAIL"
        outcome = classify_drm_igt(_transcript(results), expected_debian_release="13.6")
        self.assertFalse(outcome.passed)

    def test_classifier_rejects_skipped_required_test(self) -> None:
        results = {name: "PASS" for name in IGT_REQUIRED_PASS}
        results["syncobj_basic"] = "SKIP"
        outcome = classify_drm_igt(_transcript(results), expected_debian_release="13.6")
        self.assertFalse(outcome.passed)

    def test_classifier_rejects_missing_summary(self) -> None:
        results = {name: "PASS" for name in IGT_REQUIRED_PASS}
        outcome = classify_drm_igt(
            _transcript(results, done=None), expected_debian_release="13.6"
        )
        self.assertFalse(outcome.passed)

    def test_classifier_rejects_kernel_panic(self) -> None:
        results = {name: "PASS" for name in IGT_REQUIRED_PASS}
        outcome = classify_drm_igt(
            _transcript(results) + b"\nKernel panic - not syncing\n",
            expected_debian_release="13.6",
        )
        self.assertFalse(outcome.passed)


if __name__ == "__main__":
    unittest.main()
