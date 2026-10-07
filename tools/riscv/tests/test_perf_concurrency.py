# SPDX-License-Identifier: MPL-2.0

from pathlib import Path
import subprocess
import unittest

from tools.riscv.perf.collect_samples import parse_sample


class ConcurrencyTests(unittest.TestCase):
    def test_schbench_stderr_selects_request_not_wakeup_or_rps(self):
        script = Path('tools/riscv/perf/debian/concurrency_case.sh').read_text()
        command = ('/nix/store/czasgjl1wx0jc4kx576n9gd156kp1wkw-'
                   'schbench-riscv64-unknown-linux-gnu-v1.0/bin/schbench '
                   '-F 256 -n 5 -r 10 -i 20')
        fake = "printf 'Wakeup Latencies percentiles\\n * 99.0th: 123\\nRequest Latencies percentiles\\n * 99.0th: 456\\nRPS percentiles\\n * 99.0th: 789\\n' >&2"
        self.assertIn(command, script)
        result = subprocess.run(['sh', '-s', 'schbench'],
                                input=script.replace(command, '{ ' + fake + '; }'),
                                text=True, capture_output=True, check=True)
        self.assertEqual(parse_sample(result.stdout), 456)


if __name__ == '__main__':
    unittest.main()
