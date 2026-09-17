#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""One-session capture tests for the Firefox composite workload."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest

from tools.riscv.debian.rootfs.browser_composite_capture import (
    CompositeCaptureError,
    DOCUMENT_SETUP_TIMEOUT_SECONDS,
    SAMPLE_SCHEDULES,
    capture_composite,
    run_composite_capture,
    workload_url,
)
from tools.riscv.debian.rootfs.browser_workload_contract import (
    MODES,
    PHASES,
    expected_phase_metrics,
)


BASE = "http://10.0.2.2:17894/browser-quality/index.html"
WORKLOAD = "http://10.0.2.2:17894/browser-quality/workload.html"
RUN_ID = "0123456789abcdef0123456789abcdef"
WORKLOAD_RUN = f"{WORKLOAD}?run={RUN_ID}"


def phase(name: str, index: int, state: str = "complete") -> dict[str, object]:
    expected = expected_phase_metrics("smoke", name)
    return {
        "name": name,
        "state": state,
        "startMs": index * 20,
        "endMs": None if state == "running" else index * 20 + 10,
        "metrics": {
            "operationCount": expected["operationCount"],
            "requestCount": expected["requestCount"],
            "contextCount": expected["contextCount"],
            "longFrameCount": 0,
            "frameMs": [4.0] * expected["frameSamples"],
        },
    }


def snapshot(count: int, *, terminal: str = "running") -> dict[str, object]:
    phases = [phase(name, index) for index, name in enumerate(PHASES[:count])]
    if terminal == "running" and phases:
        phases[-1] = phase(phases[-1]["name"], count - 1, "running")
    return {
        "schemaVersion": 1,
        "workloadVersion": 1,
        "clockDomain": "browser-performance-now",
        "runId": RUN_ID,
        "mode": "smoke",
        "state": terminal,
        "phases": phases,
        "error": "workload-failed" if terminal == "failed" else None,
    }


class FakeMarionette:
    def __init__(
        self,
        snapshots: list[dict[str, object]] | None = None,
        *,
        snapshot_url: str | None = None,
    ) -> None:
        self.url = "about:blank"
        self.snapshots = snapshots or [
            snapshot(3),
            snapshot(len(PHASES), terminal="complete"),
        ]
        self.snapshot_url = snapshot_url
        self.commands: list[str] = []
        self.timeouts: list[float] = []

    def set_timeout(self, value: float) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        pass

    def command(self, name: str, parameters: object | None = None) -> object:
        self.commands.append(name)
        if name == "WebDriver:NewSession":
            return {
                "value": {
                    "sessionId": "composite-session",
                    "capabilities": {"acceptInsecureCerts": False},
                }
            }
        if name == "WebDriver:GetWindowHandles":
            return {"value": ["window-1"]}
        if name == "WebDriver:SwitchToWindow":
            assert parameters == {"handle": "window-1", "focus": False}
            return {"value": None}
        if name == "WebDriver:Navigate":
            assert isinstance(parameters, dict)
            self.url = str(parameters["url"])
            return {"value": None}
        if name == "WebDriver:ExecuteScript":
            assert isinstance(parameters, dict)
            script = str(parameters["script"])
            if "document.readyState" in script:
                return {
                    "value": json.dumps({"url": self.url, "readyState": "complete"})
                }
            if "__asterinasStartCompositeWorkload" in script:
                return {"value": "started"}
            if "__asterinasCompositeWorkloadSnapshot" in script:
                value = (
                    self.snapshots.pop(0)
                    if len(self.snapshots) > 1
                    else self.snapshots[0]
                )
                return {
                    "value": json.dumps(
                        {"url": self.snapshot_url or self.url, "workload": value}
                    )
                }
        raise AssertionError(f"unexpected command {name}")


class BrowserCompositeCaptureTests(unittest.TestCase):
    @staticmethod
    def publish_covered_sample(path: Path, *_args: object) -> None:
        ready, stop = _args[-2:]
        assert isinstance(ready, threading.Event)
        assert isinstance(stop, threading.Event)
        start_ns = time.monotonic_ns()
        ready.set()
        stop.wait(1)
        end_ns = max(time.monotonic_ns(), start_ns + 1)
        path.write_text(
            json.dumps(
                {
                    "intervals": [
                        {
                            "guest_monotonic_start_ns": start_ns,
                            "guest_monotonic_end_ns": end_ns,
                        }
                    ]
                }
            )
        )

    def test_document_setup_budget_is_separate_and_bounded(self) -> None:
        self.assertEqual(DOCUMENT_SETUP_TIMEOUT_SECONDS, 120.0)
        for mode, (interval_seconds, samples) in SAMPLE_SCHEDULES.items():
            self.assertGreaterEqual(
                interval_seconds * samples, MODES[mode]["deadline_seconds"]
            )

    def test_workload_url_requires_exact_local_fixture_origin(self) -> None:
        self.assertEqual(workload_url(BASE), WORKLOAD)
        self.assertEqual(workload_url(BASE, RUN_ID), WORKLOAD_RUN)
        with self.assertRaises(CompositeCaptureError):
            workload_url(BASE, "not-a-run-id")
        for invalid in (
            "https://10.0.2.2:17894/browser-quality/index.html",
            "http://example.com:17894/browser-quality/index.html",
            BASE + "?secret=1",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(CompositeCaptureError),
            ):
                workload_url(invalid)

    def test_capture_records_each_completed_phase_once(self) -> None:
        observations: list[dict[str, object]] = []
        checkpoints: list[dict[str, object]] = []
        ticks = iter(range(1_000, 20_000))

        report = capture_composite(
            FakeMarionette(),
            BASE,
            mode="smoke",
            run_id=RUN_ID,
            timeout_seconds=30,
            checkpoint_fn=lambda value: checkpoints.append(value),
            clock_ns=lambda: next(ticks),
            sleep_fn=lambda _seconds: None,
        )
        observations.extend(report["phase_observations"])

        self.assertEqual(report["workload"]["state"], "complete")
        self.assertEqual([item["phase"] for item in observations], list(PHASES))
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[-1]["completed_phases"], list(PHASES))

    def test_capture_starts_workload_only_after_evidence_is_ready(self) -> None:
        events: list[str] = []

        class OrderedMarionette(FakeMarionette):
            def command(self, name: str, parameters: object | None = None) -> object:
                if name == "WebDriver:ExecuteScript" and isinstance(parameters, dict):
                    if "__asterinasStartCompositeWorkload" in str(parameters["script"]):
                        events.append("workload-start")
                return super().command(name, parameters)

        report = capture_composite(
            OrderedMarionette(),
            BASE,
            mode="smoke",
            run_id=RUN_ID,
            timeout_seconds=30,
            before_start_fn=lambda: events.append("evidence-ready"),
            clock_ns=iter(range(1_000, 20_000)).__next__,
            sleep_fn=lambda _seconds: None,
        )

        self.assertEqual(events, ["evidence-ready", "workload-start"])
        self.assertLessEqual(
            report["workload_start_observed_guest_monotonic_ns"],
            report["phase_observations"][-1]["observed_guest_monotonic_ns"],
        )

    def test_capture_rejects_mutated_completed_phase_prefix(self) -> None:
        first = snapshot(3)
        terminal = snapshot(len(PHASES), terminal="complete")
        terminal["phases"][0]["endMs"] = 9
        with self.assertRaisesRegex(CompositeCaptureError, "completed phase changed"):
            capture_composite(
                FakeMarionette([first, terminal]),
                BASE,
                mode="smoke",
                run_id=RUN_ID,
                timeout_seconds=30,
                sleep_fn=lambda _seconds: None,
            )

    def test_capture_rejects_failed_or_reordered_workload(self) -> None:
        failed = snapshot(3, terminal="failed")
        failed["phases"][-1]["state"] = "failed"
        reordered = snapshot(3)
        reordered["phases"][0], reordered["phases"][1] = (
            reordered["phases"][1],
            reordered["phases"][0],
        )
        for value in (failed, reordered):
            with self.subTest(value=value), self.assertRaises(CompositeCaptureError):
                capture_composite(
                    FakeMarionette([value]),
                    BASE,
                    mode="smoke",
                    run_id=RUN_ID,
                    timeout_seconds=30,
                    sleep_fn=lambda _seconds: None,
                )

    def test_capture_rejects_changed_document_and_keeps_prior_checkpoint(self) -> None:
        checkpoints: list[dict[str, object]] = []
        with self.assertRaises(CompositeCaptureError):
            capture_composite(
                FakeMarionette(snapshot_url="http://10.0.2.2:17894/other"),
                BASE,
                mode="smoke",
                run_id=RUN_ID,
                timeout_seconds=30,
                checkpoint_fn=checkpoints.append,
                sleep_fn=lambda _seconds: None,
            )
        self.assertEqual(checkpoints, [])

        failed = snapshot(4, terminal="failed")
        failed["phases"][-1]["state"] = "failed"
        with self.assertRaises(CompositeCaptureError):
            capture_composite(
                FakeMarionette([snapshot(3), failed]),
                BASE,
                mode="smoke",
                run_id=RUN_ID,
                timeout_seconds=30,
                checkpoint_fn=checkpoints.append,
                sleep_fn=lambda _seconds: None,
            )
        self.assertEqual(checkpoints[-1]["completed_phases"], list(PHASES[:3]))

    def test_run_capture_publishes_private_artifacts_without_deleting_session(
        self,
    ) -> None:
        client = FakeMarionette()

        def system_sample(
            path: Path,
            pids: tuple[int, int],
            _mode: str,
            ready: threading.Event,
            stop: threading.Event,
        ) -> None:
            self.publish_covered_sample(path, pids, ready, stop)

        def thread_sample(
            path: Path,
            pid: int,
            marker: Path,
            mode: str,
            physical: bool,
            ready: threading.Event,
            stop: threading.Event,
        ) -> None:
            self.publish_covered_sample(path, pid, ready, stop)
            value = json.loads(path.read_text())
            value.update(
                {
                    "process_id": pid,
                    "mode": mode,
                    "physical": physical,
                    "ready": marker.is_file(),
                }
            )
            path.write_text(json.dumps(value))

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            report = run_composite_capture(
                client,
                BASE,
                firefox_pid=116,
                xorg_pid=75,
                evidence_dir=output,
                mode="smoke",
                timeout_seconds=30,
                physical=True,
                system_sample_fn=system_sample,
                thread_sample_fn=thread_sample,
                identity_fn=lambda _pids: (100, 200),
                run_id_fn=lambda: RUN_ID,
                sleep_fn=lambda _seconds: None,
            )

            expected = (
                "browser-composite-capture.json",
                "browser-composite-checkpoint.json",
                "browser-system-time.json",
                "browser-thread-time.json",
            )
            for name in expected:
                path = output / name
                self.assertTrue(path.is_file(), name)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(
                json.loads((output / "browser-thread-time.json").read_text())["ready"]
            )
            self.assertEqual(report["firefox_pid"], 116)
            self.assertTrue(report["physical"])

        self.assertNotIn("WebDriver:DeleteSession", client.commands)
        self.assertIn("WebDriver:SwitchToWindow", client.commands)

    def test_run_capture_rejects_existing_output_and_pid_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "browser-composite-capture.json").write_text("owned")
            with self.assertRaises(CompositeCaptureError):
                run_composite_capture(
                    FakeMarionette(),
                    BASE,
                    firefox_pid=116,
                    xorg_pid=75,
                    evidence_dir=output,
                    mode="smoke",
                    timeout_seconds=30,
                    system_sample_fn=lambda *_args: None,
                    thread_sample_fn=lambda *_args: None,
                    identity_fn=lambda _pids: (100, 200),
                    run_id_fn=lambda: RUN_ID,
                    sleep_fn=lambda _seconds: None,
                )

        identities = iter(((100, 200), (101, 200)))
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(CompositeCaptureError),
        ):
            run_composite_capture(
                FakeMarionette(),
                BASE,
                firefox_pid=116,
                xorg_pid=75,
                evidence_dir=Path(directory),
                mode="smoke",
                timeout_seconds=30,
                system_sample_fn=self.publish_covered_sample,
                thread_sample_fn=self.publish_covered_sample,
                identity_fn=lambda _pids: next(identities),
                run_id_fn=lambda: RUN_ID,
                sleep_fn=lambda _seconds: None,
            )

    def test_run_capture_propagates_either_sampler_failure(self) -> None:
        def fail(*_args: object) -> None:
            raise RuntimeError("sampler failed")

        publish = self.publish_covered_sample

        for system_fn, thread_fn in ((fail, publish), (publish, fail)):
            with (
                self.subTest(system=system_fn is fail),
                tempfile.TemporaryDirectory() as directory,
            ):
                with self.assertRaises(CompositeCaptureError):
                    run_composite_capture(
                        FakeMarionette(),
                        BASE,
                        firefox_pid=116,
                        xorg_pid=75,
                        evidence_dir=Path(directory),
                        mode="smoke",
                        timeout_seconds=30,
                        system_sample_fn=system_fn,
                        thread_sample_fn=thread_fn,
                        identity_fn=lambda _pids: (100, 200),
                        run_id_fn=lambda: RUN_ID,
                        sleep_fn=lambda _seconds: None,
                    )

    def test_run_capture_rejects_sampler_that_ends_before_workload(self) -> None:
        def uncovered(path: Path, *_args: object) -> None:
            ready, stop = _args[-2:]
            assert isinstance(ready, threading.Event)
            assert isinstance(stop, threading.Event)
            ready.set()
            stop.wait(1)
            path.write_text(
                json.dumps(
                    {
                        "intervals": [
                            {
                                "guest_monotonic_start_ns": 0,
                                "guest_monotonic_end_ns": 1,
                            }
                        ]
                    }
                )
            )

        for system_fn, thread_fn, expected_role in (
            (uncovered, self.publish_covered_sample, "system"),
            (self.publish_covered_sample, uncovered, "thread"),
        ):
            with (
                self.subTest(role=expected_role),
                tempfile.TemporaryDirectory() as directory,
                self.assertRaisesRegex(
                    CompositeCaptureError,
                    rf"composite {expected_role} evidence does not cover workload "
                    r"evidence=\[0,1\] workload=\[[0-9]+,[0-9]+\]",
                ),
            ):
                run_composite_capture(
                    FakeMarionette(),
                    BASE,
                    firefox_pid=116,
                    xorg_pid=75,
                    evidence_dir=Path(directory),
                    mode="smoke",
                    timeout_seconds=30,
                    system_sample_fn=system_fn,
                    thread_sample_fn=thread_fn,
                    identity_fn=lambda _pids: (100, 200),
                    run_id_fn=lambda: RUN_ID,
                    sleep_fn=lambda _seconds: None,
                )

    def test_run_capture_bounds_a_sampler_that_ignores_stop(self) -> None:
        release = threading.Event()

        def stuck(_path: Path, *_args: object) -> None:
            ready = _args[-2]
            assert isinstance(ready, threading.Event)
            ready.set()
            release.wait(1)

        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                started = time.monotonic()
                with self.assertRaisesRegex(CompositeCaptureError, "stop expired"):
                    run_composite_capture(
                        FakeMarionette(),
                        BASE,
                        firefox_pid=116,
                        xorg_pid=75,
                        evidence_dir=output,
                        mode="smoke",
                        timeout_seconds=30,
                        system_sample_fn=stuck,
                        thread_sample_fn=stuck,
                        identity_fn=lambda _pids: (100, 200),
                        run_id_fn=lambda: RUN_ID,
                        sampler_stop_timeout_seconds=0.01,
                        sleep_fn=lambda _seconds: None,
                    )
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertFalse(any(output.glob(".browser-composite-samplers.*")))
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
