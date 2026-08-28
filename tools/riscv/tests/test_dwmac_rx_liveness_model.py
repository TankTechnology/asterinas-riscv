from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_SOURCE = REPOSITORY_ROOT / "tools/riscv/dwmac_rx_liveness_model.rs"
POLL_SOURCE = REPOSITORY_ROOT / "kernel/comps/dwmac/src/poll.rs"


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
        self.assertIn("raise-timer", report["prefix"])
        self.assertIn("raise-tx", report["prefix"])
        self.assertIn("dma-complete", report["cycle"])
        self.assertIn("poll-consume", report["cycle"])
        self.assertLessEqual(len(report["prefix"]) + len(report["cycle"]), 12)
        self.assertEqual(report["cycle"].count("dma-complete"), 2)
        self.assertEqual(report["cycle"].count("poll-consume"), 2)
        self.assertNotIn("service-timer", report["cycle"])
        self.assertNotIn("service-tx", report["cycle"])

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

    def test_bounded_protocol_has_no_starvation_or_lost_wakeup(self) -> None:
        result = self.run_model("bounded")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["protocol"], "bounded")
        self.assertEqual(report["verdict"], "verified-within-model")
        self.assertEqual(
            report["properties"],
            [
                "descriptor-ownership",
                "bounded-rx-poll",
                "eventual-rearm-or-reschedule",
                "no-lost-rx-wakeup",
                "tx-timer-progress",
            ],
        )
        self.assertGreater(report["explored_states"], 0)

    def test_all_reduced_ring_sizes_are_verified(self) -> None:
        for ring_size in ("2", "3", "4"):
            with self.subTest(ring_size=ring_size):
                result = subprocess.run(
                    [
                        str(self.binary),
                        "--protocol",
                        "bounded",
                        "--ring-size",
                        ring_size,
                        "--json",
                    ],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_counterexample_is_deterministic(self) -> None:
        first = self.run_model("current")
        second = self.run_model("current")
        self.assertEqual(first.returncode, 1)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stderr, second.stderr)


class DwmacRxPollContractTests(unittest.TestCase):
    def test_production_poll_budget_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "dwmac-rx-poll-tests"
            compile_result = subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    "-Dwarnings",
                    "--test",
                    str(POLL_SOURCE),
                    "-o",
                    str(binary),
                ],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [str(binary), "--nocapture"],
                cwd=REPOSITORY_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("4 passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
