# SPDX-License-Identifier: MPL-2.0

"""Static contract tests for Debian-chroot LMBench runners."""

from pathlib import Path
import unittest


PERF = Path(__file__).resolve().parents[1] / "perf"


class DebianChrootLmbenchTests(unittest.TestCase):
    def test_runner_declares_and_uses_debian_chroot(self):
        text = (PERF / "debian/lmbench_chroot_case.sh").read_text()
        self.assertIn('LMBENCH_EXECUTION_USERSPACE=debian_chroot', text)
        self.assertIn('"$chroot_bin" "$root" /bin/sh -c', text)
        self.assertNotIn('/benchmark/bin/lmbench/', text)

    def test_sample_wrapper_can_require_userspace_marker(self):
        text = (PERF / "run_lmbench_sample.sh").read_text()
        self.assertIn("EXPECTED_USERSPACE", text)
        self.assertIn("LMBENCH_EXECUTION_USERSPACE=$expected_userspace", text)

    def test_rootfs_preparer_requires_debian_interpreter(self):
        text = (PERF / "debian/prepare_lmbench_rootfs.sh").read_text()
        self.assertIn("ld-linux-riscv64-lp64d.so.1", text)
        self.assertIn("debugfs -w", text)


if __name__ == "__main__":
    unittest.main()
