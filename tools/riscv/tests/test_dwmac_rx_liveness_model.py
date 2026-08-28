from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_SOURCE = REPOSITORY_ROOT / "tools/riscv/dwmac_rx_liveness_model.rs"


class DwmacRxLivenessModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls._temporary_directory.name) / "dwmac-rx-model"
        subprocess.run(
            [
                "rustc",
                "--edition=2024",
                "-Dwarnings",
                str(MODEL_SOURCE),
                "-o",
                str(cls.binary),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary_directory.cleanup()

    def run_model(self, protocol: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.binary), "--protocol", protocol, "--ring-size", "2", "--json"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_current_protocol_reports_starvation_counterexample(self) -> None:
        result = self.run_model("current")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["protocol"], "current")
        self.assertEqual(report["verdict"], "counterexample")
        self.assertEqual(report["property"], "bounded-rx-poll")
        self.assertGreater(len(report["prefix"]), 0)
        self.assertIn("dma-complete", report["cycle"])
        self.assertIn("poll-consume", report["cycle"])

    def test_cli_rejects_noncanonical_arguments(self) -> None:
        for arguments in (
            [],
            ["--protocol", "unknown", "--ring-size", "2", "--json"],
            ["--protocol", "current", "--ring-size", "1", "--json"],
            ["--protocol", "current", "--ring-size", "5", "--json"],
            ["--protocol", "current", "--ring-size", "02", "--json"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [str(self.binary), *arguments],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
