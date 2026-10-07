# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import unittest

from tools.riscv.perf.aggregate_results import aggregate


class AggregateResultsTests(unittest.TestCase):
    def test_reports_statistics_and_a_ratio(self) -> None:
        result = aggregate(
            {
                "schema_version": 1,
                "benchmark": "stat",
                "unit": "us",
                "direction": "lower_is_better",
                "samples": {
                    "asterinas": [18, 20, 22, 24, 26],
                    "linux": [10, 10, 10, 10, 10],
                },
                "configuration": {"smp": 4, "release": True},
            }
        )
        self.assertEqual(result["systems"]["asterinas"]["median"], 22)
        self.assertEqual(result["systems"]["linux"]["p95"], 10)
        self.assertEqual(result["ratio_asterinas_over_linux"]["median"], 2.2)
        self.assertEqual(result["configuration"]["smp"], 4)

    def test_requires_repeated_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least three"):
            aggregate(
                {
                    "schema_version": 1,
                    "benchmark": "getpid",
                    "unit": "us",
                    "samples": {"asterinas": [1, 2], "linux": [1, 2, 3]},
                }
            )

    def test_rejects_non_finite_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            aggregate(
                {
                    "schema_version": 1,
                    "benchmark": "fio",
                    "unit": "MB/s",
                    "samples": {
                        "asterinas": [1, float("nan"), 3],
                        "linux": [1, 2, 3],
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
