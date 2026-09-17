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
                "metrics": {
                    "firefoxPid": 101,
                    "bootFirefoxExecNs": 1_000_000_000,
                    "bootFirstWindowReadyNs": 1_500_000_000,
                    "durationMs": 500.0,
                },
                "reason": None,
            },
            {
                "name": "input",
                "state": "pass",
                "clockDomain": "browser-performance-now",
                "metrics": {
                    "keyboard": {
                        "firstRaf": {"p50Ms": 25.0, "p95Ms": 100.0},
                        "nextRaf": {"p50Ms": 30.0, "p95Ms": 100.0},
                    },
                    "pointer": {
                        "firstRaf": {"p50Ms": 25.0, "p95Ms": 100.0},
                        "nextRaf": {"p50Ms": 30.0, "p95Ms": 100.0},
                    },
                },
                "reason": None,
            },
            {
                "name": "scroll",
                "state": "pass",
                "clockDomain": "browser-performance-now",
                "metrics": {
                    "firstRaf": {"p50Ms": 25.0, "p95Ms": 100.0},
                    "nextRaf": {"p50Ms": 30.0, "p95Ms": 100.0},
                },
                "reason": None,
            },
            {
                "name": "navigation",
                "state": "pass",
                "clockDomain": "multiple-clock-domains-separated",
                "metrics": {
                    "localCommand": {
                        "clockDomain": "guest-monotonic",
                        "durationMs": 500.0,
                    },
                    "browserNavigation": {
                        "clockDomain": "browser-navigation",
                        "fetchStartMs": 10.0,
                        "responseToDomMs": 2_000.0,
                        "responseToLoadMs": 2_100.0,
                    },
                },
                "reason": None,
            },
            {
                "name": "context-switch",
                "state": "pass",
                "clockDomain": "guest-monotonic",
                "metrics": {
                    "openMs": 100.0,
                    "selectMs": 100.0,
                    "returnMs": 100.0,
                    "closeMs": 100.0,
                    "totalMs": 400.0,
                    "handleCountBefore": 1,
                    "handleCountAfter": 1,
                },
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

    def test_rejects_malformed_run_id_and_attribution_references(self) -> None:
        for run_id in ("A" * 32, "a" * 31, "g" * 32):
            result = complete_result()
            result["runId"] = run_id
            with self.subTest(run_id=run_id), self.assertRaises(DailyUseContractError):
                validate_daily_use_result(result)

        missing = complete_result()
        missing["attribution"]["systemArtifact"] = "missing.json"
        missing_field = complete_result()
        missing_field["attribution"].pop("systemArtifact")
        duplicate = complete_result()
        duplicate["attribution"]["threadArtifact"] = duplicate["attribution"][
            "systemArtifact"
        ]
        for result in (missing, missing_field, duplicate):
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
            result["performance"][1]["metrics"]["keyboard"]["firstRaf"][
                "p95Ms"
            ] = value
            with self.subTest(label=label), self.assertRaises(DailyUseContractError):
                validate_daily_use_result(result)

    def test_rejects_invalid_or_identity_changing_processes(self) -> None:
        for field, value in (("pid", 0), ("startTimeTicks", 0)):
            result = complete_result()
            result["identities"]["firefox"]["initial"][field] = value
            with self.subTest(field=field), self.assertRaises(DailyUseContractError):
                validate_daily_use_result(result)

        result = complete_result()
        for snapshot in ("initial", "final"):
            result["identities"]["xorg"][snapshot]["pid"] = result["identities"][
                "firefox"
            ][snapshot]["pid"]
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

        result = complete_result()
        result["identities"]["firefox"]["final"]["startTimeTicks"] += 1
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

    def test_rejects_cross_clock_metric_evidence(self) -> None:
        result = complete_result()
        result["performance"][3]["metrics"]["localCommand"][
            "clockDomain"
        ] = "browser-navigation"

        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

    def test_threshold_boundaries_derive_slow_states_and_count(self) -> None:
        result = complete_result()
        for index in (1, 2, 3, 4):
            result["performance"][index]["state"] = "slow"
        result["performance"][1]["metrics"]["keyboard"]["firstRaf"][
            "p95Ms"
        ] = 100.001
        result["performance"][2]["metrics"]["nextRaf"]["p95Ms"] = 100.001
        result["performance"][3]["metrics"]["browserNavigation"][
            "responseToDomMs"
        ] = 2_000.001
        result["performance"][4]["metrics"]["openMs"] = 500.001
        result["performance"][4]["metrics"]["totalMs"] = 800.001
        result["slowCount"] = 4

        self.assertEqual(validate_daily_use_result(result)["slowCount"], 4)

        result = complete_result()
        result["performance"][1]["state"] = "slow"
        result["performance"][1]["metrics"]["keyboard"]["firstRaf"][
            "p95Ms"
        ] = 100.0
        result["slowCount"] = 1
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

        result = complete_result()
        result["performance"][1]["state"] = "slow"
        result["performance"][1]["metrics"]["keyboard"]["firstRaf"][
            "p95Ms"
        ] = 100.001
        result["slowCount"] = True
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

    def test_requires_complete_nested_performance_evidence(self) -> None:
        missing = complete_result()
        missing["performance"][1]["metrics"]["keyboard"]["firstRaf"].pop(
            "p50Ms"
        )
        extra = complete_result()
        extra["performance"][2]["metrics"]["nextRaf"]["extra"] = 1.0
        startup_duration = complete_result()
        startup_duration["performance"][0]["metrics"]["durationMs"] = 501.0
        startup_pid = complete_result()
        startup_pid["performance"][0]["metrics"]["firefoxPid"] = 999
        startup_endpoints = complete_result()
        startup_endpoints["performance"][0]["metrics"]["bootFirefoxExecNs"] = (
            1_600_000_000
        )
        context_total = complete_result()
        context_total["performance"][4]["metrics"]["totalMs"] = 401.0
        context_handles = complete_result()
        context_handles["performance"][4]["metrics"]["handleCountAfter"] = 2

        for result in (
            missing,
            extra,
            startup_duration,
            startup_pid,
            startup_endpoints,
            context_total,
            context_handles,
        ):
            with self.subTest(result=result), self.assertRaises(DailyUseContractError):
                validate_daily_use_result(result)

        reordered_navigation = complete_result()
        reordered_navigation["performance"][3]["metrics"]["browserNavigation"][
            "responseToLoadMs"
        ] = 1_999.0
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(reordered_navigation)

    def test_preserves_invalid_navigation_timing_as_unsupported(self) -> None:
        result = complete_result()
        result["performance"][3] = {
            "name": "navigation",
            "state": "unsupported",
            "clockDomain": "multiple-clock-domains-separated",
            "metrics": {
                "localCommand": {
                    "clockDomain": "guest-monotonic",
                    "durationMs": 500.0,
                },
                "browserNavigation": {
                    "clockDomain": "browser-navigation",
                    "fetchStartMs": -1.0,
                    "responseToDomMs": None,
                    "responseToLoadMs": None,
                },
            },
            "reason": "navigation-timing-invalid",
        }
        self.assertEqual(
            validate_daily_use_result(result)["performance"][3]["state"],
            "unsupported",
        )

        result["performance"][3]["state"] = "pass"
        result["performance"][3]["reason"] = None
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

        result = complete_result()
        result["artifacts"] = [
            {"name": f"artifact-{index}.json", "bytes": 1, "sha256": "a" * 64}
            for index in range(17)
        ]
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

        result = complete_result()
        result["artifacts"] = result["artifacts"][:2]
        with self.assertRaises(DailyUseContractError):
            validate_daily_use_result(result)

    def test_rejects_invalid_oversized_and_duplicate_limitations(self) -> None:
        invalid = complete_result()
        invalid["limitations"] = {"items": ["made-up-limitation"]}
        oversized = complete_result()
        oversized["limitations"] = {"items": ["synthetic-input-timing"] * 17}
        duplicate = complete_result()
        duplicate["limitations"] = {
            "items": ["synthetic-input-timing", "synthetic-input-timing"]
        }

        for result in (invalid, oversized, duplicate):
            with self.subTest(result=result), self.assertRaises(DailyUseContractError):
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
