#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the conservative three-run physical daily-use report."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.riscv.megrez_firefox_daily_use_report import (
    ReportError,
    build_report,
    main,
)
from tools.riscv.tests.test_browser_daily_use_contract import complete_result


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


class DailyUseReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_run(
        self,
        index: int,
        *,
        wait_ratio: float = 0.30,
        main_runtime: float = 0.70,
        firefox_runtime: float = 0.80,
        wall: float = 1.0,
        hottest_tid: int = 101,
        threshold_slow: bool = False,
        coverage: bool = True,
        identity_change: tuple[str, object] | None = None,
        fixture_port: int = 17894,
    ) -> Path:
        directory = self.root / f"run-{index}-{len(list(self.root.iterdir()))}"
        directory.mkdir(mode=0o700)
        experiment_id = f"{index + 1:032x}"
        gate_run_id = f"{index + 101:032x}"
        result = complete_result()
        result["runId"] = gate_run_id
        for phase in result["performance"]:
            metrics = phase["metrics"]
            if phase["name"] == "input":
                for device in ("keyboard", "pointer"):
                    metrics[device]["firstRaf"].update(p50Ms=50.0, p95Ms=100.0)
                    metrics[device]["nextRaf"].update(p50Ms=50.0, p95Ms=100.0)
            elif phase["name"] == "scroll":
                metrics["firstRaf"].update(p50Ms=50.0, p95Ms=100.0)
                metrics["nextRaf"].update(p50Ms=50.0, p95Ms=100.0)
            elif phase["name"] == "navigation":
                metrics["localCommand"]["durationMs"] = 100.0
                metrics["browserNavigation"].update(
                    responseToDomMs=100.0, responseToLoadMs=110.0
                )
            elif phase["name"] == "context-switch":
                metrics.update(
                    openMs=25.0,
                    selectMs=25.0,
                    returnMs=25.0,
                    closeMs=25.0,
                    totalMs=100.0,
                )
        if threshold_slow:
            result["performance"][1]["metrics"]["keyboard"]["firstRaf"]["p95Ms"] = 101.0
            result["performance"][1].update(state="slow", reason=None)
            result["slowCount"] = 1
        evidence = {
            "browser-daily-use-result.json": canonical(result),
            "browser-composite-capture.json": canonical(
                {
                    "run_id": gate_run_id,
                    "workload_start_observed_guest_monotonic_ns": 200,
                    "phase_observations": [
                        {
                            "phase": f"phase-{phase}",
                            "observed_guest_monotonic_ns": 300 + phase,
                        }
                        for phase in range(7)
                    ],
                    "workload": {
                        "phases": [
                            {
                                "name": f"phase-{phase}",
                                "startMs": phase * 10.0,
                                "endMs": (phase + 1) * 10.0,
                            }
                            for phase in range(7)
                        ]
                    },
                }
            ),
        }
        start = 100 if coverage else 250
        end = 500
        process_user = firefox_runtime * 1000.0
        evidence["browser-system-time.json"] = canonical(
            {
                "intervals": [
                    {
                        "guest_monotonic_start_ns": start,
                        "guest_monotonic_end_ns": end,
                        "duration_ms": wall * 1000.0,
                        "processes": [
                            {
                                "pid": 101,
                                "cpu_user_ms": process_user,
                                "cpu_kernel_ms": 0.0,
                            },
                            {"pid": 202, "cpu_user_ms": 100.0, "cpu_kernel_ms": 0.0},
                        ],
                        "system": {
                            "context_switches": 40,
                            "per_cpu": [
                                {"cpu_id": cpu, "busy_fraction": 0.5}
                                for cpu in range(4)
                            ],
                        },
                    }
                ]
            }
        )
        wait = main_runtime * wait_ratio / max(1.0 - wait_ratio, 1e-9)
        main_thread = {
            "tid": 101,
            "comm": "firefox-esr",
            "schedstat": {
                "delta": {
                    "cpu_runtime_ns": int(main_runtime * 1_000_000_000),
                    "runqueue_wait_ns": int(wait * 1_000_000_000),
                    "dispatch_count": 12,
                }
            },
        }
        threads = [main_thread]
        if hottest_tid != 101:
            threads.append(
                {
                    "tid": hottest_tid,
                    "comm": "Renderer",
                    "schedstat": {
                        "delta": {
                            "cpu_runtime_ns": int((main_runtime + 0.2) * 1_000_000_000),
                            "runqueue_wait_ns": 0,
                            "dispatch_count": 5,
                        }
                    },
                }
            )
        evidence["browser-thread-time.json"] = canonical(
            {
                "process_id": 101,
                "intervals": [
                    {
                        "guest_monotonic_start_ns": start,
                        "guest_monotonic_end_ns": end,
                        "duration_ms": wall * 1000.0,
                        "threads": threads,
                    }
                ],
            }
        )
        result["artifacts"] = [
            {
                "name": name,
                "bytes": len(evidence[name]),
                "sha256": hashlib.sha256(evidence[name]).hexdigest(),
            }
            for name in (
                "browser-composite-capture.json",
                "browser-system-time.json",
                "browser-thread-time.json",
            )
        ]
        evidence["browser-daily-use-result.json"] = canonical(result)
        deployment = {
            "commit": "a" * 40,
            "profile": "debian-browser",
            "hartCount": 4,
            "smp": 4,
            "sv39": True,
            "serialDevice": "/dev/serial/by-id/usb-test",
            "displayProvider": "fbdev",
            "browserPackage": "firefox-esr@packages-lock:" + "9" * 64,
            "artifacts": {
                "kernel": "1" * 64,
                "initramfs": "2" * 64,
                "megrez_dtb": "3" * 64,
                "root_manifest": "4" * 64,
                "packages_lock": "9" * 64,
            },
            "transport": ["kernel:cached"],
        }
        if identity_change is not None:
            deployment[identity_change[0]] = identity_change[1]
        fixture = {
            "bindAddress": "10.100.19.216",
            "port": fixture_port,
            "allowedPeer": "10.100.19.200",
            "payloadBytes": 65536,
            "payloadSha256": "7" * 64,
        }
        artifact_rows = [
            {
                "name": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in evidence.items()
        ]
        run_result = {
            "schema_version": 1,
            "experiment_id": experiment_id,
            "gate_run_id": gate_run_id,
            "passed": True,
            "qualified": True,
            "reason": "qualified",
            "recovered": True,
            "plan_sha256": "8" * 64,
            "bootargs_sha256": "6" * 64,
            "fixture": fixture,
            "deployment": {
                key: value for key, value in deployment.items() if key != "transport"
            },
            "terminal": {
                "experiment_id": experiment_id,
                "outcome": "pass",
                "gate_status": 0,
                "upload_status": 0,
            },
            "artifacts": artifact_rows,
        }
        files = {
            **evidence,
            "run-result.json": canonical(run_result),
            "deployment.json": canonical(deployment),
            "fixture-summary.json": canonical({"bytes": 1234, "sha256": "5" * 64}),
            "serial.log": b"serial evidence\n",
        }
        for name, payload in files.items():
            path = directory / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
        manifest = {
            "schemaVersion": 1,
            "files": [
                {
                    "name": name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for name, payload in files.items()
            ],
        }
        manifest_path = directory / "sha256-manifest.json"
        descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(manifest))
        return directory

    def three(self, **kwargs) -> list[Path]:
        return [self.make_run(index, **kwargs) for index in range(3)]

    def test_report_requires_three_distinct_qualified_runs(self) -> None:
        run_a, run_b, _run_c = self.three()
        with self.assertRaisesRegex(ReportError, "three distinct"):
            build_report([run_a, run_b, run_b])

    def test_runnable_delay_requires_recurrence_in_all_runs(self) -> None:
        report = build_report(self.three(wait_ratio=0.30))
        self.assertEqual(report["classification"], "runnable-delayed")
        self.assertEqual(report["classificationAgreement"], 3)

    def test_one_outlier_cannot_admit_a_mechanism(self) -> None:
        runs = [
            self.make_run(index, wait_ratio=value)
            for index, value in enumerate((0.30, 0.04, 0.03))
        ]
        report = build_report(runs)
        self.assertEqual(report["classification"], "mixed")

    def test_executing_sleeping_and_nonleader_boundaries(self) -> None:
        executing = build_report(
            self.three(wait_ratio=0.10, main_runtime=0.80, firefox_runtime=1.0)
        )
        self.assertEqual(executing["classification"], "executing")
        sleeping = build_report(
            self.three(wait_ratio=0.10, main_runtime=0.05, firefox_runtime=0.10)
        )
        self.assertEqual(sleeping["classification"], "sleeping-blocking")
        nonleader = build_report(
            self.three(
                wait_ratio=0.10,
                main_runtime=0.80,
                firefox_runtime=1.0,
                hottest_tid=303,
            )
        )
        self.assertEqual(nonleader["classification"], "mixed")

    def test_incomplete_coverage_and_manifest_drift_are_rejected(self) -> None:
        runs = self.three()
        broken_coverage = self.make_run(4, coverage=False)
        with self.assertRaisesRegex(ReportError, "coverage"):
            build_report([runs[0], runs[1], broken_coverage])
        (runs[2] / "serial.log").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ReportError, "digest|size"):
            build_report(runs)

    def test_repeated_ids_and_mixed_immutable_identity_are_rejected(self) -> None:
        runs = self.three()
        duplicate = self.make_run(0)
        with self.assertRaisesRegex(ReportError, "distinct"):
            build_report([runs[0], runs[1], duplicate])
        changed = self.make_run(9, identity_change=("commit", "b" * 40))
        with self.assertRaisesRegex(ReportError, "immutable"):
            build_report([runs[0], runs[1], changed])

    def test_every_declared_immutable_identity_must_be_stable(self) -> None:
        baseline = self.three()
        changes = (
            ("serialDevice", "/dev/serial/by-id/usb-other"),
            ("hartCount", 8),
            ("displayProvider", "other"),
            ("browserPackage", "firefox-esr@other"),
            (
                "artifacts",
                {
                    "kernel": "f" * 64,
                    "initramfs": "2" * 64,
                    "megrez_dtb": "3" * 64,
                    "root_manifest": "4" * 64,
                    "packages_lock": "9" * 64,
                },
            ),
        )
        for field, value in changes:
            with self.subTest(field=field):
                changed = self.make_run(10, identity_change=(field, value))
                with self.assertRaisesRegex(ReportError, "immutable"):
                    build_report([baseline[0], baseline[1], changed])
        changed_fixture = self.make_run(11, fixture_port=17895)
        with self.assertRaisesRegex(ReportError, "immutable"):
            build_report([baseline[0], baseline[1], changed_fixture])

    def test_one_threshold_signal_does_not_override_recurring_attribution(self) -> None:
        runs = [
            self.make_run(
                index,
                wait_ratio=0.10,
                main_runtime=0.05,
                firefox_runtime=0.10,
                threshold_slow=index == 0,
            )
            for index in range(3)
        ]
        report = build_report(runs)
        self.assertEqual(report["classification"], "sleeping-blocking")
        self.assertEqual(report["diagnosticThresholdRuns"], 1)

    def test_report_retains_raw_values_and_summarizes_without_outlier_removal(self):
        runs = [
            self.make_run(index, wait_ratio=value)
            for index, value in enumerate((0.20, 0.30, 0.40))
        ]
        report = build_report(runs)
        summary = report["metrics"]["keyboardFirstRafP95Ms"]
        self.assertEqual(summary["values"], [100.0, 100.0, 100.0])
        self.assertEqual(
            (summary["minimum"], summary["median"], summary["maximum"]),
            (100.0, 100.0, 100.0),
        )
        self.assertEqual(len(report["runs"]), 3)
        self.assertIn("raw", report["runs"][0])

    def test_cli_writes_private_exclusive_json_and_markdown(self) -> None:
        runs = self.three()
        json_output = self.root / "report.json"
        markdown_output = self.root / "report.md"
        self.assertEqual(
            main(
                [
                    *(str(path) for path in runs),
                    "--json-output",
                    str(json_output),
                    "--markdown-output",
                    str(markdown_output),
                ]
            ),
            0,
        )
        for path in (json_output, markdown_output):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        markdown = markdown_output.read_text(encoding="utf-8")
        self.assertIn("not USB-to-HDMI latency", markdown)
        self.assertIn("procfs placeholder fault fields were not used", markdown)
        with self.assertRaises(FileExistsError):
            main(
                [
                    *(str(path) for path in runs),
                    "--json-output",
                    str(json_output),
                    "--markdown-output",
                    str(self.root / "second.md"),
                ]
            )


if __name__ == "__main__":
    unittest.main()
