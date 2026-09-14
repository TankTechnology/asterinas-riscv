# SPDX-License-Identifier: MPL-2.0

"""Contract tests for bounded Firefox interaction latency evidence."""

from __future__ import annotations

import math
import unittest

from tools.riscv.debian.rootfs import browser_interaction_perf as perf


class InteractionTimingTests(unittest.TestCase):
    def test_summarizes_bounded_samples_with_nearest_rank_percentiles(self) -> None:
        result = perf.summarize_input_latencies([12.5, 8.0, 20.0, 10.0])

        self.assertEqual(
            result,
            {
                "count": 4,
                "min_ms": 8.0,
                "p50_ms": 10.0,
                "p95_ms": 20.0,
                "max_ms": 20.0,
            },
        )

    def test_rejects_invalid_sample_collections(self) -> None:
        invalid_samples = (
            None,
            (),
            [],
            [True],
            ["1.0"],
            [math.nan],
            [math.inf],
            [0.0],
            [-1.0],
            [60_001.0],
            [1.0] * 65,
        )

        for samples in invalid_samples:
            with self.subTest(samples=samples):
                with self.assertRaises(perf.PerformanceContractError):
                    perf.summarize_input_latencies(samples)

    def test_accepts_integer_and_float_samples_without_mutating_input(self) -> None:
        samples = [3, 1.25, 2]

        result = perf.summarize_input_latencies(samples)

        self.assertEqual(samples, [3, 1.25, 2])
        self.assertEqual(result["min_ms"], 1.25)
        self.assertEqual(result["p50_ms"], 2.0)
        self.assertEqual(result["p95_ms"], 3.0)


if __name__ == "__main__":
    unittest.main()
