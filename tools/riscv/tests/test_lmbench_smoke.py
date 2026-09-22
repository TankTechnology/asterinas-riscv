# SPDX-License-Identifier: MPL-2.0

import sys
import unittest

from tools.riscv.lmbench_smoke import Case, parse_measurement, run_case


class LmbenchSmokeTests(unittest.TestCase):
    def test_decimal_megabytes_for_one_mib(self):
        self.assertEqual(parse_measurement('size', '1.05 12419.34\n'), 12419.34)
        self.assertEqual(parse_measurement('size', '1.048576 147\n'), 147)

    def test_reject_invalid_or_unrelated_output(self):
        for output in ['', 'Usage: bw_mem ...', '1.05 nan', '1.05 0',
                       '1.05 -1', '512.00 12', '1.05 12\nread: failed']:
            with self.subTest(output=output):
                self.assertIsNone(parse_measurement('size', output))

    def test_context_header_and_result(self):
        self.assertEqual(parse_measurement('context', '\n"size=0k ovr=10.00\n2 18.39\n'), 18.39)
        self.assertIsNone(parse_measurement('context', '2 18.39\nerror'))

    def test_exact_latency_label(self):
        self.assertEqual(parse_measurement('Simple syscall', 'Simple syscall: 4.2493 microseconds\n'), 4.2493)
        self.assertIsNone(parse_measurement('Simple syscall', 'Simple read: 4 microseconds\n'))

    def run_python(self, source, timeout=2):
        case = Case('probe', ('-c', source), 'Simple syscall', 'us')
        return run_case(case, sys.executable, {}, timeout)

    def test_exit_zero_without_measurement_fails(self):
        self.assertFalse(self.run_python('pass')['passed'])

    def test_measurement_on_stderr_passes(self):
        result = self.run_python('import sys; print("Simple syscall: 4 microseconds", file=sys.stderr)')
        self.assertTrue(result['passed'])
        self.assertEqual(result['value'], 4)

    def test_nonzero_exit_with_measurement_fails(self):
        self.assertFalse(self.run_python('print("Simple syscall: 4 microseconds"); raise SystemExit(1)')['passed'])

    def test_timeout_kills_child_holding_output_pipe(self):
        result = self.run_python('import os,time; os.fork(); time.sleep(10)', timeout=.1)
        self.assertTrue(result['timeout'])
        self.assertFalse(result['passed'])
        self.assertLess(result['elapsed'], 3)


if __name__ == '__main__':
    unittest.main()
