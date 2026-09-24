# SPDX-License-Identifier: MPL-2.0

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SAMPLER = Path(__file__).resolve().parents[1] / "debian/rootfs/thread_pc_sampler.py"
CHILD_CODE = (
    "import ctypes, time; "
    "assert ctypes.CDLL(None).prctl(0x59616d61, -1, 0, 0, 0) == 0; "
    "time.sleep(10)"
)


class ThreadPcSamplerTests(unittest.TestCase):
    def test_attach_samples_and_detaches_a_live_thread(self) -> None:
        with subprocess.Popen([sys.executable, "-c", CHILD_CODE]) as child:
            try:
                comm = Path(f"/proc/{child.pid}/comm").read_text().strip()
                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "samples.jsonl"
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SAMPLER),
                            "--pid",
                            str(child.pid),
                            "--comm",
                            comm,
                            "--samples",
                            "3",
                            "--interval-ms",
                            "20",
                            "--output",
                            str(output),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=8,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    records = [json.loads(line) for line in output.read_text().splitlines()]
                    self.assertEqual(records[0]["kind"], "metadata")
                    samples = [record for record in records if record["kind"] == "sample"]
                    self.assertEqual(len(samples), 3)
                    self.assertTrue(all(sample["tid"] == child.pid for sample in samples))
                    self.assertTrue(all(sample["pc"] > 0 for sample in samples))
                    self.assertIsNone(child.poll(), "tracee must run after detach")
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_missing_thread_name_fails_without_stopping_process(self) -> None:
        with subprocess.Popen([sys.executable, "-c", CHILD_CODE]) as child:
            try:
                with tempfile.TemporaryDirectory() as directory:
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SAMPLER),
                            "--pid",
                            str(child.pid),
                            "--comm",
                            "missing-thread-name",
                            "--samples",
                            "1",
                            "--output",
                            str(Path(directory) / "samples.jsonl"),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("no matching threads", result.stderr)
                    self.assertIsNone(child.poll())
            finally:
                child.terminate()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
