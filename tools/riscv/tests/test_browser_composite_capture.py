#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""One-session capture tests for the Firefox composite workload."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

from tools.riscv.debian.rootfs.browser_composite_capture import (
    CompositeCaptureError,
    capture_composite,
    run_composite_capture,
    workload_url,
)
from tools.riscv.debian.rootfs.browser_workload_contract import PHASES


BASE = "http://10.0.2.2:17894/browser-quality/index.html"
WORKLOAD = "http://10.0.2.2:17894/browser-quality/workload.html"


def phase(name: str, index: int, state: str = "complete") -> dict[str, object]:
    return {
        "name": name,
        "state": state,
        "startMs": index * 20,
        "endMs": None if state == "running" else index * 20 + 10,
        "metrics": {
            "operationCount": 10,
            "requestCount": 2,
            "contextCount": 0,
            "longFrameCount": 0,
            "frameMs": [4.0],
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
        snapshot_url: str = WORKLOAD,
    ) -> None:
        self.url = "about:blank"
        self.snapshots = snapshots or [snapshot(3), snapshot(len(PHASES), terminal="complete")]
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
        if name == "WebDriver:Navigate":
            assert isinstance(parameters, dict)
            self.url = str(parameters["url"])
            return {"value": None}
        if name == "WebDriver:ExecuteScript":
            assert isinstance(parameters, dict)
            script = str(parameters["script"])
            if "document.readyState" in script:
                return {
                    "value": json.dumps(
                        {"url": self.url, "readyState": "complete"}
                    )
                }
            if "__asterinasStartCompositeWorkload" in script:
                return {"value": "started"}
            if "__asterinasCompositeWorkloadSnapshot" in script:
                value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
                return {
                    "value": json.dumps(
                        {"url": self.snapshot_url, "workload": value}
                    )
                }
        raise AssertionError(f"unexpected command {name}")


class BrowserCompositeCaptureTests(unittest.TestCase):
    def test_workload_url_requires_exact_local_fixture_origin(self) -> None:
        self.assertEqual(workload_url(BASE), WORKLOAD)
        for invalid in (
            "https://10.0.2.2:17894/browser-quality/index.html",
            "http://example.com:17894/browser-quality/index.html",
            BASE + "?secret=1",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                CompositeCaptureError
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
                timeout_seconds=30,
                checkpoint_fn=checkpoints.append,
                sleep_fn=lambda _seconds: None,
            )
        self.assertEqual(checkpoints[-1]["completed_phases"], list(PHASES[:3]))

    def test_run_capture_publishes_private_artifacts_without_deleting_session(
        self,
    ) -> None:
        client = FakeMarionette()

        def system_sample(path: Path, pids: tuple[int, int], _mode: str) -> None:
            path.write_text(json.dumps({"process_ids": list(pids)}))

        def thread_sample(
            path: Path,
            pid: int,
            marker: Path,
            mode: str,
            physical: bool,
        ) -> None:
            path.write_text(
                json.dumps(
                    {
                        "process_id": pid,
                        "mode": mode,
                        "physical": physical,
                        "ready": marker.is_file(),
                    }
                )
            )

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
                    sleep_fn=lambda _seconds: None,
                )

        identities = iter(((100, 200), (101, 200)))
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(
            CompositeCaptureError
        ):
            run_composite_capture(
                FakeMarionette(),
                BASE,
                firefox_pid=116,
                xorg_pid=75,
                evidence_dir=Path(directory),
                mode="smoke",
                timeout_seconds=30,
                system_sample_fn=lambda path, *_args: path.write_text("{}"),
                thread_sample_fn=lambda path, *_args: path.write_text("{}"),
                identity_fn=lambda _pids: next(identities),
                sleep_fn=lambda _seconds: None,
            )

    def test_run_capture_propagates_either_sampler_failure(self) -> None:
        def fail(*_args: object) -> None:
            raise RuntimeError("sampler failed")

        def publish(path: Path, *_args: object) -> None:
            path.write_text("{}")

        for system_fn, thread_fn in ((fail, publish), (publish, fail)):
            with self.subTest(system=system_fn is fail), tempfile.TemporaryDirectory() as directory:
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
                        sleep_fn=lambda _seconds: None,
                    )


if __name__ == "__main__":
    unittest.main()
