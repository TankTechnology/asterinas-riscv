"""Keep the TLB probe's syscall and reader-drain timings separate."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "diagnostics" / "tlb_shootdown_probe.c"


class TlbShootdownProbeTimingTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("gcc"), "native gcc is unavailable")
    def test_reports_mprotect_syscalls_separately_from_reader_join(self):
        with tempfile.TemporaryDirectory(prefix="asterinas-tlb-probe-test-") as tmp:
            binary = Path(tmp) / "probe"
            subprocess.run(
                ["gcc", "-O2", "-pthread", str(SOURCE), "-o", str(binary)],
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                [str(binary)], check=True, capture_output=True, text=True, timeout=20
            )

        self.assertIn("TLB_PROBE phase=mprotect syscalls=24 wall=", result.stdout)
        self.assertIn("TLB_PROBE phase=mprotect reader_join wall=", result.stdout)
        self.assertIn("TLB_PROBE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
