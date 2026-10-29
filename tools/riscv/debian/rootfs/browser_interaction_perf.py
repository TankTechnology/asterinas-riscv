#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Validate and summarize bounded Firefox interaction latency evidence."""

from __future__ import annotations

import math


MAX_INPUT_SAMPLES = 64
MAX_INPUT_LATENCY_MS = 60_000.0


class PerformanceContractError(ValueError):
    """Browser performance evidence violated its bounded schema."""


def _nearest_rank(values: list[float], percentile: int) -> float:
    rank = max(1, math.ceil(percentile * len(values) / 100))
    return values[rank - 1]


def summarize_input_latencies(samples: object) -> dict[str, int | float]:
    """Returns bounded nearest-rank statistics for input latency samples."""

    if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_INPUT_SAMPLES:
        raise PerformanceContractError("input latency sample count is out of bounds")

    values: list[float] = []
    for sample in samples:
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            raise PerformanceContractError("input latency sample is not numeric")
        value = float(sample)
        if not math.isfinite(value) or not 0.0 < value <= MAX_INPUT_LATENCY_MS:
            raise PerformanceContractError("input latency sample is out of bounds")
        values.append(value)

    values.sort()
    return {
        "count": len(values),
        "min_ms": values[0],
        "p50_ms": _nearest_rank(values, 50),
        "p95_ms": _nearest_rank(values, 95),
        "max_ms": values[-1],
    }
