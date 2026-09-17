#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""One-session orchestration tests with deterministic browser operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.riscv.debian.rootfs import browser_daily_use_gate as gate
from tools.riscv.debian.rootfs.browser_daily_use_gate import (
    ARTIFACT_NAMES,
    PHASES,
    DailyUseGateError,
    DailyUseOperations,
    FixtureCapture,
    TimingCapture,
    ContextCapture,
    CompositeCapture,
    SamplerCapture,
    run_daily_use_gate,
)
from tools.riscv.tests.test_browser_daily_use_contract import RUN_ID, complete_result


class FakeMarionette:
    def __init__(self, events):
        self.events = events
        self.handles = ["original"]
        self.selected = "original"
        self.closed = False
        self.cleanup_failure = False
        self.secure = True

    def set_timeout(self, timeout):
        self.timeout = timeout

    def command(self, name, parameters=None):
        self.events.append(name)
        if name == "WebDriver:NewSession":
            assert parameters["acceptInsecureCerts"] is False
            return {
                "value": {
                    "sessionId": "session",
                    "capabilities": {
                        "acceptInsecureCerts": not self.secure,
                    },
                }
            }
        if name == "WebDriver:GetWindowHandle":
            return {"value": self.selected}
        if name == "WebDriver:GetWindowHandles":
            return {"value": list(self.handles)}
        if name == "WebDriver:SwitchToWindow":
            self.selected = parameters["handle"]
            return {"value": None}
        if name == "WebDriver:NewWindow":
            self.handles.append("second")
            return {"value": {"handle": "second", "type": "tab"}}
        if name == "WebDriver:CloseWindow":
            if self.cleanup_failure:
                raise RuntimeError("private browser diagnostic")
            self.handles.remove(self.selected)
            return {"value": list(self.handles)}
        raise AssertionError(name)

    def close(self):
        self.events.append("transport-close")
        self.closed = True


class BrowserDailyUseGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.evidence = Path(self.temporary.name)
        self.events = []
        self.client = FakeMarionette(self.events)
        self.source = complete_result()
        self.ready = {"system": threading.Event(), "thread": threading.Event()}
        self.stop_release = threading.Event()
        self.addCleanup(self.stop_release.set)
        self.identity_calls = 0
        self.operations = DailyUseOperations(
            fixture=self.fixture,
            local_timing=self.timing,
            context_switch=self.context,
            composite=self.composite,
            system_sampler=lambda request: self.sample("system", request),
            thread_sampler=lambda request: self.sample("thread", request),
            identity_reader=self.identities,
        )

    def identities(self, pids):
        self.identity_calls += 1
        self.events.append("identity")
        self.assertEqual(pids, (101, 202))
        return (1000, 2000)

    def fixture(self, request):
        self.assertTrue(all(event.is_set() for event in self.ready.values()))
        self.assertEqual(request.original_window, "original")
        self.assertEqual(
            request.fixture_index_url,
            "http://10.0.2.2:17894/browser-quality/index.html",
        )
        self.events.append("fixture")
        self.assertFalse((self.evidence / "browser-daily-use-result.json").exists())
        return FixtureCapture(self.source["functionGroups"], b"fixture")

    def timing(self, request):
        self.events.append("local-timing")
        return TimingCapture(self.source["performance"][:4], b"timing")

    def context(self, request):
        self.events.append("context-switch")
        response = request.client.command("WebDriver:NewWindow", {"type": "tab"})
        request.client.command(
            "WebDriver:SwitchToWindow", {"handle": response["value"]["handle"]}
        )
        request.client.command("WebDriver:SwitchToWindow", {"handle": "original"})
        return ContextCapture(self.source["performance"][4], b"context")

    def composite(self, request):
        self.events.append("composite")
        return CompositeCapture(b"composite", self.source["limitations"])

    def sample(self, name, request):
        start = time.monotonic_ns()
        self.events.append(name + "-ready")
        self.ready[name].set()
        request.ready.set()
        request.stop.wait(2)
        self.events.append(name + "-final")
        return SamplerCapture(name.encode(), start, time.monotonic_ns())

    def run_gate(self, **overrides):
        arguments = dict(
            client=self.client,
            operations=self.operations,
            firefox_pid=101,
            xorg_pid=202,
            evidence_dir=self.evidence,
            mode="smoke",
            timeout_seconds=0.2,
            run_id=RUN_ID,
            fixture_index_url="http://10.0.2.2:17894/browser-quality/index.html",
        )
        arguments.update(overrides)
        return run_daily_use_gate(**arguments)

    def checkpoint(self, reason):
        self.assert_no_canonical_captures()
        path = self.evidence / "browser-daily-use-checkpoint.json"
        value = json.loads(path.read_text())
        self.assertEqual(
            set(value),
            {"schemaVersion", "runId", "completedPhases", "functionGroups", "failure"},
        )
        self.assertEqual(value["schemaVersion"], 1)
        self.assertEqual(value["runId"], RUN_ID)
        self.assertEqual(
            value["completedPhases"], list(PHASES[: len(value["completedPhases"])])
        )
        self.assertEqual(value["failure"], {"type": "daily-use-gate", "reason": reason})
        self.assertEqual(
            value["functionGroups"],
            self.source["functionGroups"]
            if "fixture" in value["completedPhases"]
            else [],
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.client.closed)
        self.assertNotIn("WebDriver:DeleteSession", self.events)
        return value

    def assert_no_canonical_captures(self):
        for name in (*ARTIFACT_NAMES, gate.RESULT_NAME):
            self.assertFalse(os.path.lexists(self.evidence / name), name)

    def test_success_preserves_one_session_and_order_and_publishes_hashed_artifacts(
        self,
    ):
        result = self.run_gate()
        self.assertEqual(result["state"], "pass")
        self.assertEqual(self.events.count("WebDriver:NewSession"), 1)
        self.assertNotIn("WebDriver:DeleteSession", self.events)
        self.assertEqual(self.client.handles, ["original"])
        self.assertEqual(self.client.selected, "original")
        self.assertTrue(self.client.closed)
        ordered = [
            "identity",
            "WebDriver:NewSession",
            "WebDriver:GetWindowHandle",
            "fixture",
            "local-timing",
            "context-switch",
            "composite",
            "WebDriver:CloseWindow",
            "WebDriver:GetWindowHandle",
            "identity",
            "transport-close",
        ]
        self.assertEqual([event for event in self.events if event in ordered], ordered)
        for name in ("system", "thread"):
            self.assertLess(
                self.events.index(name + "-ready"), self.events.index("fixture")
            )
            self.assertGreater(
                self.events.index(name + "-final"), self.events.index("composite")
            )
            self.assertLess(
                self.events.index(name + "-final"),
                self.events.index("WebDriver:CloseWindow"),
            )
        for artifact in result["artifacts"]:
            path = self.evidence / artifact["name"]
            self.assertEqual(
                artifact["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            self.assertEqual(artifact["bytes"], path.stat().st_size)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path = self.evidence / "browser-daily-use-result.json"
        self.assertEqual(json.loads(path.read_text()), result)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.evidence / "browser-daily-use-checkpoint.json").exists())

    def test_failure_closes_second_window_and_publishes_bounded_checkpoint(self):
        def fail(request):
            raise RuntimeError("sensitive " * 1000)

        self.operations = replace(self.operations, composite=fail)
        with self.assertRaisesRegex(DailyUseGateError, "phase-failed"):
            self.run_gate()
        self.checkpoint("phase-failed")
        self.assertEqual(self.client.handles, ["original"])
        self.assertEqual(self.client.selected, "original")

    def test_cleanup_failure_blocks_result_and_preserves_failure_reason(self):
        self.client.cleanup_failure = True
        with self.assertRaisesRegex(DailyUseGateError, "cleanup-failed"):
            self.run_gate()
        self.checkpoint("cleanup-failed")

    def test_identity_change_blocks_result(self):
        def identities(pids):
            return (
                (1000 + self.identity_calls, 2000)
                if self.identity_calls
                else self.identities(pids)
            )

        self.operations = replace(self.operations, identity_reader=identities)
        with self.assertRaisesRegex(DailyUseGateError, "identity-changed"):
            self.run_gate()
        self.checkpoint("identity-changed")

    def test_insecure_session_is_rejected(self):
        self.client.secure = False
        with self.assertRaisesRegex(DailyUseGateError, "session-invalid"):
            self.run_gate()
        self.checkpoint("session-invalid")
        self.assertNotIn("fixture", self.events)

    def test_callback_cannot_create_or_delete_session(self):
        for command in ("WebDriver:NewSession", "WebDriver:DeleteSession"):
            with self.subTest(command=command):

                def attempt(request):
                    request.client.command(command)

                self.operations = replace(self.operations, fixture=attempt)
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(DailyUseGateError):
                        self.run_gate(evidence_dir=Path(directory))
                self.assertEqual(
                    self.events.count(command), int(command.endswith("NewSession"))
                )
                self.events.clear()

    def test_extra_missing_and_reordered_contract_phase_values_are_rejected(self):
        for mutation in (
            lambda entries: entries + [entries[0]],
            lambda entries: entries[:-1],
            lambda entries: entries[::-1],
        ):
            for field in ("functionGroups", "performance"):
                with (
                    self.subTest(field=field),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    self.source = complete_result()
                    if field == "functionGroups":
                        self.source[field] = mutation(self.source[field])
                        self.operations = replace(
                            self.operations, local_timing=self.timing
                        )
                    else:
                        self.operations = replace(
                            self.operations,
                            local_timing=lambda request: TimingCapture(
                                mutation(self.source["performance"][:4]), b"timing"
                            ),
                        )
                    with self.assertRaises(DailyUseGateError):
                        self.run_gate(evidence_dir=Path(directory))
                    self.assertFalse(
                        (Path(directory) / "browser-daily-use-result.json").exists()
                    )

    def test_callback_cannot_inject_an_unbounded_failure_reason(self):
        def fail(request):
            raise DailyUseGateError("sensitive " * 1000)

        self.operations = replace(self.operations, fixture=fail)
        with self.assertRaisesRegex(DailyUseGateError, "phase-failed"):
            self.run_gate()
        self.checkpoint("phase-failed")

    def test_publication_fsync_failure_cannot_leave_a_success_result(self):
        real_fsync = os.fsync
        failed = False

        def fail_once(fd):
            nonlocal failed
            if (
                self.evidence / "browser-daily-use-result.json"
            ).exists() and not failed:
                failed = True
                raise OSError("injected directory sync failure")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=fail_once):
            with self.assertRaises(DailyUseGateError):
                self.run_gate()
        self.checkpoint("phase-failed")

    def test_later_component_publication_failure_retracts_earlier_components(self):
        real_publish = gate._publish

        def fail(source, destination):
            if destination.name == ARTIFACT_NAMES[2]:
                raise OSError("injected component publication failure")
            real_publish(source, destination)

        with mock.patch.object(gate, "_publish", side_effect=fail):
            with self.assertRaisesRegex(DailyUseGateError, "phase-failed"):
                self.run_gate()
        self.checkpoint("phase-failed")

    def test_checkpoint_persistence_failure_preserves_canonical_outward_error(self):
        real_write = gate._write_private

        def fail(path, payload):
            if path.name in (gate.RESULT_NAME, gate.CHECKPOINT_NAME):
                raise OSError("persistence unavailable")
            real_write(path, payload)

        with mock.patch.object(gate, "_write_private", side_effect=fail):
            with self.assertRaisesRegex(DailyUseGateError, "^phase-failed$"):
                self.run_gate()
        self.assert_no_canonical_captures()
        self.assertFalse((self.evidence / gate.CHECKPOINT_NAME).exists())

    def test_checkpoint_link_failure_preserves_canonical_outward_error(self):
        real_link = os.link

        def fail(source, destination, **kwargs):
            if Path(destination).name in (gate.RESULT_NAME, gate.CHECKPOINT_NAME):
                raise OSError("publication unavailable")
            real_link(source, destination, **kwargs)

        with mock.patch("os.link", side_effect=fail):
            with self.assertRaisesRegex(DailyUseGateError, "^phase-failed$"):
                self.run_gate()
        self.assert_no_canonical_captures()
        self.assertFalse((self.evidence / gate.CHECKPOINT_NAME).exists())

    def test_racing_publication_destination_is_not_overwritten(self):
        original_open = os.open
        target = self.evidence / ARTIFACT_NAMES[0]
        raced = False

        def race(path, flags, *args, **kwargs):
            nonlocal raced
            if Path(path) == self.evidence and flags & os.O_DIRECTORY and not raced:
                target.write_bytes(b"racing evidence")
                raced = True
            return original_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=race):
            with self.assertRaisesRegex(DailyUseGateError, "^evidence-exists$"):
                self.run_gate()
        self.assertEqual(target.read_bytes(), b"racing evidence")
        for name in (*ARTIFACT_NAMES[1:], gate.RESULT_NAME):
            self.assertFalse((self.evidence / name).exists())

    def test_unsafe_directory_retains_exact_failure_reason(self):
        self.evidence.chmod(0o755)
        try:
            with self.assertRaisesRegex(
                DailyUseGateError, "^evidence-directory-invalid$"
            ):
                self.run_gate()
        finally:
            self.evidence.chmod(0o700)

    def test_browser_timeout_exception_uses_canonical_phase_timeout(self):
        def timeout(request):
            raise TimeoutError("private timeout detail")

        self.operations = replace(self.operations, local_timing=timeout)
        with self.assertRaisesRegex(DailyUseGateError, "^phase-timeout$"):
            self.run_gate()
        self.checkpoint("phase-timeout")

    def test_injected_clock_rejects_late_phase_return_without_sleeping(self):
        now = [1.0]
        clock = gate.DailyUseClock(lambda: now[0], lambda: int(now[0] * 1_000_000_000))

        def late(request):
            now[0] = request.deadline + 1
            return self.fixture(request)

        self.operations = replace(self.operations, clock=clock, fixture=late)
        with self.assertRaisesRegex(DailyUseGateError, "^phase-timeout$"):
            self.run_gate()
        self.checkpoint("phase-timeout")

    def test_injected_clock_controls_sampler_interval_coverage(self):
        clock = gate.DailyUseClock(lambda: 2.0, lambda: 2_000_000_000)

        def sample(name, request):
            self.ready[name].set()
            request.ready.set()
            request.stop.wait(2)
            return SamplerCapture(name.encode(), 1_000_000_000, 2_000_000_000)

        self.operations = replace(
            self.operations,
            clock=clock,
            system_sampler=lambda request: sample("system", request),
            thread_sampler=lambda request: sample("thread", request),
        )
        self.assertEqual(self.run_gate()["state"], "pass")

    def test_checkpoint_retains_detached_functional_states_and_reasons(self):
        expected = complete_result()["functionGroups"]
        expected[0] = {
            "name": "document",
            "state": "fail",
            "reason": "fixture-capability-failed",
        }
        self.source["functionGroups"] = [dict(group) for group in expected]

        def fail(request):
            self.source["functionGroups"][0]["reason"] = "unbounded " * 1000
            raise RuntimeError("later phase")

        self.operations = replace(self.operations, local_timing=fail)
        with self.assertRaises(DailyUseGateError):
            self.run_gate()
        checkpoint = json.loads((self.evidence / gate.CHECKPOINT_NAME).read_text())
        self.assertEqual(checkpoint["functionGroups"], expected)
        self.assertIn("fixture", checkpoint["completedPhases"])

    def test_checkpoint_excludes_invalid_functional_values(self):
        self.source["functionGroups"][0]["extra"] = "private"
        with self.assertRaisesRegex(DailyUseGateError, "^contract-invalid$"):
            self.run_gate()
        checkpoint = self.checkpoint("contract-invalid")
        self.assertEqual(checkpoint["functionGroups"], [])
        self.assertNotIn("fixture", checkpoint["completedPhases"])

    def test_transport_close_failure_blocks_publication(self):
        original_close = self.client.close

        def fail():
            original_close()
            raise OSError("injected close failure")

        self.client.close = fail
        with self.assertRaisesRegex(DailyUseGateError, "cleanup-failed"):
            self.run_gate()
        self.checkpoint("cleanup-failed")

    def test_wrong_capture_type_and_empty_artifact_block_publication(self):
        for value, reason in (
            ({}, "phase-value-invalid"),
            (FixtureCapture(self.source["functionGroups"], b""), "artifact-invalid"),
        ):
            self.operations = replace(self.operations, fixture=lambda request: value)
            with (
                self.subTest(reason=reason),
                tempfile.TemporaryDirectory() as directory,
            ):
                with self.assertRaisesRegex(DailyUseGateError, reason):
                    self.run_gate(evidence_dir=Path(directory))

    def test_preexisting_component_symlink_is_preserved(self):
        target = self.evidence / "unrelated"
        target.write_bytes(b"preserve")
        link = self.evidence / "browser-system-time.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(DailyUseGateError, "evidence-exists"):
            self.run_gate()
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_concurrent_run_ids_cannot_both_claim_the_evidence_directory(self):
        barrier = threading.Barrier(2)
        original_iterdir = Path.iterdir

        def synchronized_iterdir(path):
            entries = list(original_iterdir(path))
            barrier.wait(timeout=2)
            return iter(entries)

        clients = [FakeMarionette([]), FakeMarionette([])]

        def run(index):
            try:
                return self.run_gate(client=clients[index], run_id=str(index) * 32)
            except DailyUseGateError:
                return None

        with mock.patch.object(Path, "iterdir", synchronized_iterdir):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(run, (0, 1)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(
            sum(client.events.count("WebDriver:NewSession") for client in clients), 1
        )
        self.assertFalse((self.evidence / "browser-daily-use-checkpoint.json").exists())

    def test_sampler_failure_or_return_before_readiness_blocks_workload(self):
        for raises in (True, False):

            def bad(request):
                # Never announce readiness: completion must be observed before
                # the orchestrator can permit any fixture workload.
                if raises:
                    raise RuntimeError("private")
                return SamplerCapture(b"early", 1, 2)

            self.operations = replace(self.operations, system_sampler=bad)
            with (
                self.subTest(raises=raises),
                tempfile.TemporaryDirectory() as directory,
            ):
                with self.assertRaisesRegex(DailyUseGateError, "sampler-failed"):
                    self.run_gate(evidence_dir=Path(directory))
                self.assertNotIn("fixture", self.events)

    def test_sampler_readiness_timeout(self):
        def unready(request):
            request.stop.wait(2)
            return SamplerCapture(b"unready", 1, 2)

        self.operations = replace(self.operations, system_sampler=unready)
        with self.assertRaisesRegex(DailyUseGateError, "sampler-ready-timeout"):
            self.run_gate(timeout_seconds=0.02)
        self.checkpoint("sampler-ready-timeout")
        self.assertNotIn("fixture", self.events)

    def test_sampler_late_failure_missing_artifact_and_coverage_failure(self):
        for outcome, reason in (
            ("exception", "sampler-failed"),
            ("missing", "artifact-invalid"),
            ("coverage", "sampler-coverage-invalid"),
        ):

            def bad(request):
                self.ready["system"].set()
                request.ready.set()
                request.stop.wait(2)
                if outcome == "exception":
                    raise RuntimeError("private")
                return SamplerCapture(None if outcome == "missing" else b"bad", 1, 2)

            self.operations = replace(self.operations, system_sampler=bad)
            with (
                self.subTest(outcome=outcome),
                tempfile.TemporaryDirectory() as directory,
            ):
                with self.assertRaisesRegex(DailyUseGateError, reason):
                    self.run_gate(evidence_dir=Path(directory))

    def test_sampler_stop_timeout_cannot_publish_late_result(self):
        def late(request):
            self.ready["system"].set()
            request.ready.set()
            self.stop_release.wait(2)
            return SamplerCapture(b"late", 1, time.monotonic_ns())

        self.operations = replace(self.operations, system_sampler=late)
        with self.assertRaisesRegex(DailyUseGateError, "sampler-stop-timeout"):
            self.run_gate(timeout_seconds=0.02)
        self.checkpoint("sampler-stop-timeout")
        self.stop_release.set()
        self.assertFalse((self.evidence / "browser-daily-use-result.json").exists())

    def test_rejects_invalid_parameters_and_closes_transport(self):
        cases = (
            {"firefox_pid": True},
            {"firefox_pid": 0},
            {"xorg_pid": 101},
            {"mode": "other"},
            {"timeout_seconds": 0},
            {"timeout_seconds": 121},
            {"timeout_seconds": float("nan")},
            {"run_id": "A" * 32},
            {"fixture_index_url": "https://example.com"},
            {"evidence_dir": Path("relative")},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(DailyUseGateError):
                    self.run_gate(**overrides)
                self.assertTrue(self.client.closed)
                self.assertNotIn("WebDriver:NewSession", self.events)

    def test_rejects_unsafe_evidence_paths_and_existing_artifacts(self):
        for name in (
            "browser-daily-use-result.json",
            "browser-daily-use-checkpoint.json",
            "browser-system-time.json",
            ".browser-daily-use-" + RUN_ID,
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / name
                path.write_bytes(b"preserve")
                with self.assertRaises(DailyUseGateError):
                    self.run_gate(evidence_dir=Path(directory))
                self.assertEqual(path.read_bytes(), b"preserve")
        link = self.evidence / "link"
        link.symlink_to(self.evidence, target_is_directory=True)
        with self.assertRaises(DailyUseGateError):
            self.run_gate(evidence_dir=link)
        self.evidence.chmod(0o755)
        with self.assertRaises(DailyUseGateError):
            self.run_gate()
        self.evidence.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
