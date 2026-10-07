# SPDX-License-Identifier: MPL-2.0

from pathlib import Path
import sys
import tempfile
import unittest

from tools.riscv.perf.collect_samples import collect, parse_sample


class CollectSamplesTests(unittest.TestCase):
    def test_rejects_missing_duplicate_and_invalid_samples(self):
        for output in ("", "PERF_SAMPLE=1\nPERF_SAMPLE=2\n",
                       "PERF_SAMPLE=nan\n", "PERF_SAMPLE=-1\n"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse_sample(output)

    def test_retains_warmup_but_does_not_count_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            samples = collect([sys.executable, "-c", "print('PERF_SAMPLE=2.5')"],
                              output, 1, 3, 5)
            self.assertEqual(samples, [2.5] * 3)
            self.assertTrue((output / "000-warmup.stdout").is_file())
            self.assertEqual(len(list(output.glob("*.stdout"))), 4)
            with self.assertRaises(FileExistsError):
                collect([sys.executable], output, 1, 3, 5)

    def test_failed_command_is_not_a_valid_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaisesRegex(ValueError, "failed"):
                collect([sys.executable, "-c",
                         "print('PERF_SAMPLE=1'); raise SystemExit(1)"],
                        output, 1, 3, 5)
            self.assertTrue((output / "000-warmup.stdout").is_file())
            self.assertFalse((output / "samples.json").exists())

    def test_timeout_retains_logs_without_publishing_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            with self.assertRaisesRegex(ValueError, "failed"):
                collect([sys.executable, "-c", "import time; time.sleep(10)"],
                        output, 1, 3, 0.05)
            self.assertIn('"timed_out": true',
                          (output / "000-warmup.json").read_text())
            self.assertFalse((output / "samples.json").exists())
