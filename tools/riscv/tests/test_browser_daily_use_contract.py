#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Strict tests for the bounded Firefox daily-use result contract."""

from __future__ import annotations

import copy
import math
import unittest

from tools.riscv.debian.rootfs.browser_daily_use_contract import (
    FUNCTION_GROUPS,
    PERFORMANCE_CATEGORIES,
    DailyUseContractError,
    build_daily_use_result,
    validate_daily_use_result,
)


RUN_ID = "0123456789abcdef0123456789abcdef"


def complete_result() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "runId": RUN_ID,
        "state": "pass",
        "identities": {
            "firefox": {
                "initial": {"pid": 101, "startTimeTicks": 1_000},
                "final": {"pid": 101, "startTimeTicks": 1_000},
            },
            "xorg": {
                "initial": {"pid": 202, "startTimeTicks": 2_000},
                "final": {"pid": 202, "startTimeTicks": 2_000},
            },
        },
        "functionGroups": [
            {"name": name, "state": "pass", "reason": None}
            for name in FUNCTION_GROUPS
        ],
        "performance": [
            {
                "name": "startup",
                "state": "pass",
                "clockDomain": "guest-monotonic",
                "metrics": {"durationMs": 500.0},
                "reason": None,
            },
            {
                "name": "input",
                "state": "pass",
                "clockDomain": "browser-performance-now",
                "metrics": {"p95Ms": 100.0},
                "reason": None,
            },
            {
                "name": "scroll",
                "state": "pass",
                "clockDomain": "browser-performance-now",
                "metrics": {"p95Ms": 100.0},
                "reason": None,
            },
            {
                "name": "navigation",
                "state": "pass",
                "clockDomain": "browser-navigation",
                "metrics": {"domReadyMs": 2_000.0},
                "reason": None,
            },
            {
                "name": "context-switch",
                "state": "pass",
                "clockDomain": "guest-monotonic",
                "metrics": {"durationMs": 500.0},
                "reason": None,
            },
        ],
        "slowCount": 0,
        "artifacts": [
            {
                "name": "browser-composite-capture.json",
                "bytes": 1_024,
                "sha256": "a" * 64,
            },
            {
                "name": "browser-system-time.json",
                "bytes": 2_048,
                "sha256": "b" * 64,
            },
            {
                "name": "browser-thread-time.json",
                "bytes": 4_096,
                "sha256": "c" * 64,
            },
        ],
        "attribution": {
            "compositeArtifact": "browser-composite-capture.json",
            "systemArtifact": "browser-system-time.json",
            "threadArtifact": "browser-thread-time.json",
        },
        "limitations": {"items": ["synthetic-input-timing"]},
    }


class BrowserDailyUseContractTests(unittest.TestCase):
    def test_accepts_complete_ordered_result_and_returns_a_detached_copy(self) -> None:
        result = complete_result()

        normalized = validate_daily_use_result(result)

        self.assertEqual(
            FUNCTION_GROUPS,
            tuple(item["name"] for item in normalized["functionGroups"]),
        )
        self.assertEqual(
            PERFORMANCE_CATEGORIES,
            tuple(item["name"] for item in normalized["performance"]),
        )
        self.assertEqual(normalized["state"], "pass")
        result["functionGroups"][0]["state"] = "fail"
        self.assertEqual(normalized["functionGroups"][0]["state"], "pass")

    def test_rejects_missing_extra_and_reordered_closed_schema_entries(self) -> None:
        missing = complete_result()
        missing.pop("attribution")
        extra = complete_result()
        extra["unexpected"] = None
        boolean_schema = complete_result()
        boolean_schema["schemaVersion"] = True
        reordered_groups = complete_result()
        reordered_groups["functionGroups"][0], reordered_groups["functionGroups"][1] = (
            reordered_groups["functionGroups"][1],
            reordered_groups["functionGroups"][0],
        )
        reordered_performance = complete_result()
        reordered_performance["performance"].reverse()

        for result in (
            missing,
            extra,
            boolean_schema,
            reordered_groups,
            reordered_performance,
        ):
            with self.subTest(result=result), self.assertRaises(DailyUseContractError):
                validate_daily_use_result(result)

    def test_rejects_nonfinite_negative_and_oversized_metrics(self) -> None:
        for label, value in (
            ("nan", math.nan),
            ("infinite", math.inf),
            ("negative", -1),
            ("bounded", 3_600_001),
            ("float-overflow", 10**100_000),
        ):
            result = complete_result()
            result["performance"][0]["metrics"]["durationMs"] = value
            with self.subTest(label=label), self.assertRaises(DailyUseContractError):
                validate_daily_use_result(result)

    def test_rejects_invalid_or_identity_changing_processes(self) -> None:
        for field, value in (("pid", 0), ("startTimeTicks", 0)):
            result = complete_result()
            result["identities"]["firefox"]["initial"][field] = value
            with self.subTest(field=field), self.assertRaises(DailyUseContractError):
                validate_daily_use_result(result)

        result = complete_result()
        result["identities"]["xorg"]["initial"]["pid"] = result["identities"][
            "firefox"
        ]["initial"]["pid"]
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

        result = complete_result()
        result["identities"]["firefox"]["final"]["startTimeTicks"] += 1
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

    def test_rejects_cross_clock_metric_evidence(self) -> None:
        result = complete_result()
        result["performance"][1]["clockDomain"] = "guest-monotonic"

        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

    def test_threshold_boundaries_derive_slow_states_and_count(self) -> None:
        result = complete_result()
        for index in (1, 2, 3, 4):
            result["performance"][index]["state"] = "slow"
        result["performance"][1]["metrics"]["p95Ms"] = 100.001
        result["performance"][2]["metrics"]["p95Ms"] = 100.001
        result["performance"][3]["metrics"]["domReadyMs"] = 2_000.001
        result["performance"][4]["metrics"]["durationMs"] = 500.001
        result["slowCount"] = 4

        self.assertEqual(validate_daily_use_result(result)["slowCount"], 4)

        result = complete_result()
        result["performance"][1]["state"] = "slow"
        result["performance"][1]["metrics"]["p95Ms"] = 100.0
        result["slowCount"] = 1
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

        result = complete_result()
        result["performance"][1]["state"] = "slow"
        result["performance"][1]["metrics"]["p95Ms"] = 100.001
        result["slowCount"] = True
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

    def test_rejects_bad_artifact_paths_hashes_and_duplicates(self) -> None:
        for name, digest, byte_count, duplicate in (
            ("../owned", "a" * 64, 1_024, False),
            ("nested/artifact.json", "a" * 64, 1_024, False),
            ("artifact.json", "A" * 64, 1_024, False),
            ("artifact.json", "a" * 64, -1, False),
            ("artifact.json", "a" * 64, 64 * 1024 * 1024 + 1, False),
            ("artifact.json", "a" * 64, 1_024, True),
        ):
            result = complete_result()
            result["artifacts"][0]["name"] = name
            result["artifacts"][0]["sha256"] = digest
            result["artifacts"][0]["bytes"] = byte_count
            if duplicate:
                result["artifacts"][1]["name"] = name
            with (
                self.subTest(name=name, byte_count=byte_count, duplicate=duplicate),
                self.assertRaises(DailyUseContractError),
            ):
                validate_daily_use_result(result)

    def test_derives_result_state_and_preserves_unsupported(self) -> None:
        result = complete_result()
        result["functionGroups"][0] = {
            "name": "document",
            "state": "unsupported",
            "reason": "fixture-capability-unavailable",
        }
        result["state"] = "fail"
        self.assertEqual(validate_daily_use_result(result)["state"], "fail")

        result["state"] = "pass"
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

    def test_rejects_reasons_outside_the_finite_reason_vocabularies(self) -> None:
        function_failure = complete_result()
        function_failure["functionGroups"][0] = {
            "name": "document",
            "state": "fail",
            "reason": "made-up-cause",
        }
        function_failure["state"] = "fail"

        unsupported_performance = complete_result()
        unsupported_performance["performance"][0] = {
            "name": "startup",
            "state": "unsupported",
            "clockDomain": "guest-monotonic",
            "metrics": {},
            "reason": "made-up-cause",
        }

        for result in (function_failure, unsupported_performance):
            with self.subTest(result=result), self.assertRaises(DailyUseContractError):
                validate_daily_use_result(result)

    def test_builds_only_a_valid_derived_result(self) -> None:
        source = complete_result()
        built = build_daily_use_result(
            run_id=RUN_ID,
            firefox_identity=copy.deepcopy(source["identities"]["firefox"]),
            xorg_identity=copy.deepcopy(source["identities"]["xorg"]),
            function_groups=copy.deepcopy(source["functionGroups"]),
            performance=copy.deepcopy(source["performance"]),
            artifacts=copy.deepcopy(source["artifacts"]),
            attribution=copy.deepcopy(source["attribution"]),
            limitations=copy.deepcopy(source["limitations"]),
        )

        self.assertEqual(built, validate_daily_use_result(built))
        self.assertEqual(built["state"], "pass")
        self.assertEqual(built["slowCount"], 0)


if __name__ == "__main__":
    unittest.main()
