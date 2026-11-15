#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed tests for browser-event and local-navigation time evidence."""

from __future__ import annotations

import math
import unittest

from tools.riscv.debian.rootfs.browser_latency_contract import (
    BrowserLatencyError,
    summarize_interactions,
    summarize_navigations,
    validate_navigation,
)


def sample(kind: str, source: str, first: float, following: float) -> dict[str, object]:
    return {
        "kind": kind,
        "source": source,
        "firstRafMs": first,
        "nextRafMs": following,
    }


def interaction_snapshot(samples: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "clockDomain": "browser-performance-now",
        "samples": samples,
    }


NAVIGATION = {
    "schemaVersion": 1,
    "clockDomain": "browser-navigation",
    "startTime": 0.0,
    "fetchStart": 10.0,
    "responseStart": 30.0,
    "responseEnd": 40.0,
    "domContentLoadedEventEnd": 80.0,
    "loadEventEnd": 100.0,
}


class BrowserLatencyContractTests(unittest.TestCase):
    def test_summarizes_each_kind_only_within_requested_source(self) -> None:
        samples = [
            sample("keyboard", "trusted", 10, 20),
            sample("pointer", "trusted", 15, 25),
            sample("scroll", "trusted", 30, 45),
            sample("keyboard", "synthetic", 1, 3),
            sample("pointer", "synthetic", 2, 4),
            sample("scroll", "synthetic", 3, 5),
        ]
        report = summarize_interactions(interaction_snapshot(samples), source="trusted")
        self.assertEqual(report["clock_domain"], "browser-performance-now")
        self.assertEqual(report["source"], "trusted")
        self.assertEqual(report["keyboard"]["next_raf_ms"]["p95_ms"], 20)
        self.assertEqual(report["pointer"]["first_raf_ms"]["p50_ms"], 15)
        self.assertEqual(report["scroll"]["next_raf_ms"]["max_ms"], 45)

    def test_rejects_missing_kind_mixed_source_and_malformed_samples(self) -> None:
        valid = [
            sample("keyboard", "trusted", 1, 2),
            sample("pointer", "trusted", 1, 2),
            sample("scroll", "trusted", 1, 2),
        ]
        invalid = (
            interaction_snapshot(valid[:-1]),
            interaction_snapshot(valid + [sample("pointer", "unknown", 1, 2)]),
            interaction_snapshot(valid + [sample("pointer", "trusted", math.nan, 2)]),
            interaction_snapshot(valid + [sample("pointer", "trusted", 4, 3)]),
            interaction_snapshot(valid + [sample("pointer", "trusted", 1, 60_001)]),
            interaction_snapshot(valid + [sample("pointer", "trusted", True, 2)]),
            interaction_snapshot(valid + [sample("pointer", "trusted", 1, 2)] * 64),
            {**interaction_snapshot(valid), "clockDomain": "guest-monotonic"},
            {**interaction_snapshot(valid), "schemaVersion": True},
            {**interaction_snapshot(valid), "other": 1},
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(BrowserLatencyError):
                summarize_interactions(raw, source="trusted")

    def test_navigation_reports_nonoverlapping_waterfall(self) -> None:
        parts = validate_navigation(NAVIGATION)
        self.assertEqual(parts["request_to_first_byte_ms"], 20)
        self.assertEqual(parts["body_transfer_ms"], 10)
        self.assertEqual(parts["response_to_dom_ms"], 40)
        self.assertEqual(parts["dom_to_load_ms"], 20)
        self.assertEqual(parts["total_ms"], 100)

    def test_navigation_rejects_incomplete_or_reordered_timing(self) -> None:
        invalid = (
            None,
            {**NAVIGATION, "loadEventEnd": 0},
            {**NAVIGATION, "responseStart": 50},
            {**NAVIGATION, "responseEnd": math.inf},
            {**NAVIGATION, "schemaVersion": 2},
            {**NAVIGATION, "schemaVersion": True},
            {**NAVIGATION, "clockDomain": "guest-monotonic"},
            {**NAVIGATION, "extra": 0},
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(BrowserLatencyError):
                validate_navigation(raw)

    def test_navigation_summary_uses_nearest_rank_without_mutation(self) -> None:
        entries = [NAVIGATION, {**NAVIGATION, "loadEventEnd": 120.0}]
        result = summarize_navigations(entries)
        self.assertEqual(result["total_ms"]["count"], 2)
        self.assertEqual(result["total_ms"]["p50_ms"], 100)
        self.assertEqual(result["total_ms"]["p95_ms"], 120)
        self.assertEqual(entries[0]["loadEventEnd"], 100)


if __name__ == "__main__":
    unittest.main()
