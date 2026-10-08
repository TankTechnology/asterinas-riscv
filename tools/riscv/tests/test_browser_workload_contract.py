#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Protocol tests for the deterministic Firefox composite workload."""

from __future__ import annotations

import copy
import math
import unittest

from tools.riscv.debian.rootfs.browser_workload_contract import (
    PHASES,
    WorkloadContractError,
    expected_phase_metrics,
    validate_workload_snapshot,
)

RUN_ID = "0123456789abcdef0123456789abcdef"


def phase(name: str, start: float, mode: str = "smoke") -> dict[str, object]:
    expected = expected_phase_metrics(mode, name)
    frames = [2.5] * expected["frameSamples"]
    return {
        "name": name,
        "state": "complete",
        "startMs": start,
        "endMs": start + 10,
        "metrics": {
            "operationCount": expected["operationCount"],
            "requestCount": expected["requestCount"],
            "contextCount": expected["contextCount"],
            "longFrameCount": 0,
            "frameMs": frames,
        },
    }


def complete_snapshot(mode: str = "smoke") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "workloadVersion": 1,
        "clockDomain": "browser-performance-now",
        "runId": RUN_ID,
        "mode": mode,
        "state": "complete",
        "phases": [
            phase(name, index * 20.0, mode) for index, name in enumerate(PHASES)
        ],
        "error": None,
    }


class BrowserWorkloadContractTests(unittest.TestCase):
    def test_accepts_complete_ordered_smoke_snapshot(self) -> None:
        report = validate_workload_snapshot(complete_snapshot(), expected_mode="smoke")

        self.assertEqual(tuple(item["name"] for item in report["phases"]), PHASES)
        self.assertEqual(report["state"], "complete")
        self.assertEqual(report["runId"], RUN_ID)

    def test_rejects_invalid_or_changed_run_identity(self) -> None:
        for run_id in ("", "A" * 32, "0" * 31, 7):
            snapshot = complete_snapshot()
            snapshot["runId"] = run_id
            with self.subTest(run_id=run_id), self.assertRaises(WorkloadContractError):
                validate_workload_snapshot(snapshot, expected_mode="smoke")

        with self.assertRaisesRegex(WorkloadContractError, "identity changed"):
            validate_workload_snapshot(
                complete_snapshot(),
                expected_mode="smoke",
                expected_run_id="f" * 32,
            )

    def test_accepts_bounded_stress_cache_attempt_count(self) -> None:
        snapshot = complete_snapshot("stress")

        report = validate_workload_snapshot(snapshot, expected_mode="stress")

        self.assertEqual(report["phases"][3]["metrics"]["requestCount"], 288)

    def test_rejects_completed_phase_with_inexact_work_or_frames(self) -> None:
        for phase_index, metric, delta in (
            (0, "operationCount", -1),
            (2, "requestCount", 1),
            (4, "contextCount", -1),
        ):
            snapshot = complete_snapshot()
            snapshot["phases"][phase_index]["metrics"][metric] += delta
            with (
                self.subTest(metric=metric),
                self.assertRaisesRegex(WorkloadContractError, "exact workload"),
            ):
                validate_workload_snapshot(snapshot, expected_mode="smoke")

        snapshot = complete_snapshot()
        snapshot["phases"][1]["metrics"]["frameMs"].pop()
        with self.assertRaisesRegex(WorkloadContractError, "frame sample count"):
            validate_workload_snapshot(snapshot, expected_mode="smoke")

    def test_rejects_long_frame_count_that_disagrees_with_samples(self) -> None:
        snapshot = complete_snapshot()
        snapshot["phases"][0]["metrics"]["frameMs"] = [51.0]
        with self.assertRaisesRegex(WorkloadContractError, "long frame count"):
            validate_workload_snapshot(snapshot, expected_mode="smoke")

    def test_accepts_only_an_ordered_prefix_while_running(self) -> None:
        snapshot = complete_snapshot()
        snapshot["state"] = "running"
        snapshot["phases"] = snapshot["phases"][:3]
        snapshot["phases"][-1]["state"] = "running"
        snapshot["phases"][-1]["endMs"] = None
        snapshot["phases"][-1]["metrics"]["operationCount"] //= 2
        snapshot["phases"][-1]["metrics"]["requestCount"] //= 2
        snapshot["phases"][-1]["metrics"]["frameMs"] = []

        report = validate_workload_snapshot(
            snapshot, expected_mode="smoke", allow_running=True
        )
        self.assertEqual(len(report["phases"]), 3)

        with self.assertRaises(WorkloadContractError):
            validate_workload_snapshot(snapshot, expected_mode="smoke")

    def test_rejects_reordered_duplicate_or_missing_terminal_phases(self) -> None:
        variants = []
        reordered = complete_snapshot()
        reordered["phases"][1], reordered["phases"][2] = (
            reordered["phases"][2],
            reordered["phases"][1],
        )
        variants.append(reordered)
        duplicate = complete_snapshot()
        duplicate["phases"][2]["name"] = duplicate["phases"][1]["name"]
        variants.append(duplicate)
        missing = complete_snapshot()
        missing["phases"] = missing["phases"][:-1]
        variants.append(missing)

        for snapshot in variants:
            with (
                self.subTest(snapshot=snapshot),
                self.assertRaises(WorkloadContractError),
            ):
                validate_workload_snapshot(snapshot, expected_mode="smoke")

    def test_rejects_wrong_mode_schema_clock_and_unknown_state(self) -> None:
        changes = (
            ("mode", "profile"),
            ("schemaVersion", 2),
            ("workloadVersion", 2),
            ("clockDomain", "wall-clock"),
            ("state", "done"),
        )
        for key, value in changes:
            snapshot = complete_snapshot()
            snapshot[key] = value
            with self.subTest(key=key), self.assertRaises(WorkloadContractError):
                validate_workload_snapshot(snapshot, expected_mode="smoke")

    def test_rejects_invalid_phase_times_and_counts(self) -> None:
        changes = (
            ("startMs", -1),
            ("startMs", math.nan),
            ("endMs", 3_600_001),
        )
        for key, value in changes:
            snapshot = complete_snapshot()
            snapshot["phases"][0][key] = value
            with self.subTest(key=key), self.assertRaises(WorkloadContractError):
                validate_workload_snapshot(snapshot, expected_mode="smoke")

        for key, value in (
            ("operationCount", True),
            ("requestCount", -1),
            ("requestCount", 385),
            ("contextCount", 4),
            ("longFrameCount", 257),
        ):
            snapshot = complete_snapshot()
            snapshot["phases"][0]["metrics"][key] = value
            with self.subTest(key=key), self.assertRaises(WorkloadContractError):
                validate_workload_snapshot(snapshot, expected_mode="smoke")

    def test_rejects_oversized_or_invalid_frame_samples(self) -> None:
        for samples in ([1.0] * 257, [-1.0], [60_001.0], [math.inf], [True]):
            snapshot = complete_snapshot()
            snapshot["phases"][0]["metrics"]["frameMs"] = samples
            with (
                self.subTest(samples=len(samples)),
                self.assertRaises(WorkloadContractError),
            ):
                validate_workload_snapshot(snapshot, expected_mode="smoke")

    def test_rejects_arbitrary_or_oversized_error_text(self) -> None:
        for error in ("contains spaces", "x" * 97, 7):
            snapshot = complete_snapshot()
            snapshot["state"] = "failed"
            snapshot["error"] = error
            with self.subTest(error=error), self.assertRaises(WorkloadContractError):
                validate_workload_snapshot(snapshot, expected_mode="smoke")

    def test_returns_a_detached_normalized_value(self) -> None:
        snapshot = complete_snapshot()
        report = validate_workload_snapshot(snapshot, expected_mode="smoke")
        original = copy.deepcopy(report)
        snapshot["phases"][0]["metrics"]["frameMs"][0] = 999
        self.assertEqual(report, original)


if __name__ == "__main__":
    unittest.main()
