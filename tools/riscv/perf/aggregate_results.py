#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Aggregate repeated Asterinas/Linux benchmark samples.

The input is deliberately small and tool-independent so LMBench, fio, iperf3,
and application benchmarks can all use the same statistical contract.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
from typing import Any


SCHEMA_VERSION = 1


def _samples(values: list[Any], system: str) -> list[float]:
    if len(values) < 3:
        raise ValueError(f"{system} requires at least three samples")
    result = [float(value) for value in values]
    if not all(math.isfinite(value) and value >= 0 for value in result):
        raise ValueError(f"{system} samples must be finite and non-negative")
    return result


def _summary(values: list[float]) -> dict[str, float | int]:
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "median": statistics.median(values),
        "p95": quantiles[94],
        "stddev": statistics.pstdev(values),
    }


def aggregate(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported benchmark input schema")
    benchmark = document.get("benchmark")
    unit = document.get("unit")
    if not isinstance(benchmark, str) or not benchmark:
        raise ValueError("benchmark must be a non-empty string")
    if not isinstance(unit, str) or not unit:
        raise ValueError("unit must be a non-empty string")
    samples = document.get("samples")
    if not isinstance(samples, dict):
        raise ValueError("samples must be an object")
    asterinas = _samples(samples.get("asterinas", []), "asterinas")
    linux = _samples(samples.get("linux", []), "linux")
    asterinas_summary = _summary(asterinas)
    linux_summary = _summary(linux)
    if linux_summary["median"] == 0:
        raise ValueError("Linux median must be non-zero for ratio calculation")
    ratios = {
        "median": asterinas_summary["median"] / linux_summary["median"],
        "p95": asterinas_summary["p95"] / linux_summary["p95"]
        if linux_summary["p95"]
        else None,
        "stddev": asterinas_summary["stddev"] / linux_summary["stddev"]
        if linux_summary["stddev"]
        else None,
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": benchmark,
        "unit": unit,
        "direction": document.get("direction", "lower_is_better"),
        "systems": {"asterinas": asterinas_summary, "linux": linux_summary},
        "ratio_asterinas_over_linux": ratios,
    }
    if "configuration" in document:
        result["configuration"] = document["configuration"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = aggregate(json.loads(args.input.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        parser.error(str(error))
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
