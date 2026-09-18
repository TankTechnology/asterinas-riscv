#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""One-session orchestration tests with deterministic browser operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import socket
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
from tools.riscv.tests.test_browser_composite_capture import (
    FakeMarionette as CompositeMarionette,
)
from tools.riscv.tests.test_browser_perf_capture import (
    BASE,
    NAVIGATION,
    FakeMarionette as TimingMarionette,
)
from tools.riscv.debian.rootfs import browser_web_marionette_gate as web


class FakeMarionette:
    def __init__(self, events):
        self.events = events
        self.timeouts = []
        self.handles = ["original"]
        self.selected = "original"
        self.closed = False
        self.cleanup_failure = False
        self.secure = True

    def set_timeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)

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


class PendingReplyMarionette(FakeMarionette):
    """A transport that rejects cleanup until a sent command reply is drained."""

    def __init__(self, events, failure):
        super().__init__(events)
        self.failure = failure
        self.pending_reply = False
        self.recoveries = 0

    def command(self, name, parameters=None):
        if name == "WebDriver:Navigate":
            self.events.append(name)
            if isinstance(self.failure, gate.CommandNotSentTimeout):
                raise self.failure
            self.pending_reply = True
            raise self.failure
        if self.pending_reply:
            raise RuntimeError("late command reply was not drained")
        return super().command(name, parameters)

    def recover_timed_out_command(self, timeout):
        self.events.append("recover-timed-out-command")
        self.recoveries += 1
        if not self.pending_reply:
            raise AssertionError("attempted to drain an unsent command")
        self.pending_reply = False


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
        return FixtureCapture(
            [self.source["functionGroups"][i] for i in (0, 1, 2, 3, 5)], b"fixture"
        )

    def timing(self, request):
        self.events.append("local-timing")
        return TimingCapture(
            self.source["performance"][:4], b"timing", self.source["functionGroups"][4]
        )

    def context(self, request):
        self.events.append("context-switch")
        response = request.client.command("WebDriver:NewWindow", {"type": "tab"})
        request.client.command(
            "WebDriver:SwitchToWindow", {"handle": response["value"]["handle"]}
        )
        request.client.command("WebDriver:SwitchToWindow", {"handle": "original"})
        return ContextCapture(
            self.source["performance"][4], b"context", self.source["functionGroups"][6]
        )

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
        owned = set()
        if "fixture" in value["completedPhases"]:
            owned.update(
                ("document", "storage", "execution", "rendering-media", "download")
            )
        if "local-timing" in value["completedPhases"]:
            owned.add("navigation")
        if "context-switch" in value["completedPhases"]:
            owned.add("contexts")
        self.assertEqual(
            value["functionGroups"],
            [item for item in self.source["functionGroups"] if item["name"] in owned],
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
        initial = [
            "identity",
            "WebDriver:NewSession",
            "WebDriver:GetWindowHandles",
            "WebDriver:GetWindowHandle",
            "WebDriver:SwitchToWindow",
        ]
        self.assertEqual(self.events[: len(initial)], initial)
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

    def test_physical_session_setup_has_a_separate_cold_start_budget(self):
        self.run_gate(physical=True)
        self.assertEqual(self.client.timeouts[0], 300.0)
        self.assertEqual(self.client.timeouts[1], 0.2)

    def test_failure_closes_second_window_and_publishes_bounded_checkpoint(self):
        def fail(request):
            raise RuntimeError("sensitive " * 1000)

        self.operations = replace(self.operations, composite=fail)
        with self.assertRaisesRegex(DailyUseGateError, "phase-failed"):
            self.run_gate()
        self.checkpoint("phase-failed")
        self.assertEqual(self.client.handles, ["original"])
        self.assertEqual(self.client.selected, "original")
        self.assertIn("WebDriver:CloseWindow", self.events)

    def test_outer_cleanup_drains_a_sent_fixture_timeout_before_using_transport(self):
        self.client = PendingReplyMarionette(self.events, TimeoutError("sent"))

        def timeout(request):
            request.client.command("WebDriver:Navigate")

        self.operations = replace(self.operations, fixture=timeout)
        with self.assertRaisesRegex(DailyUseGateError, "phase-timeout"):
            self.run_gate()
        self.checkpoint("phase-timeout")
        self.assertEqual(self.client.recoveries, 1)
        self.assertLess(
            self.events.index("recover-timed-out-command"),
            self.events.index("transport-close"),
        )
        self.assertEqual(self.client.handles, ["original"])

    def test_outer_cleanup_does_not_drain_an_unsent_fixture_timeout(self):
        self.client = PendingReplyMarionette(
            self.events, gate.CommandNotSentTimeout("unsent")
        )

        def timeout(request):
            request.client.command("WebDriver:Navigate")

        self.operations = replace(self.operations, fixture=timeout)
        with self.assertRaisesRegex(DailyUseGateError, "phase-timeout"):
            self.run_gate()
        self.checkpoint("phase-timeout")
        self.assertEqual(self.client.recoveries, 0)
        self.assertEqual(self.client.handles, ["original"])

    def test_preexisting_extra_window_fails_without_running_workload_or_closing_it(self):
        self.client.handles.append("pre-existing")

        def should_not_run(request):
            self.fail("workload ran despite pre-existing window")

        self.operations = replace(self.operations, fixture=should_not_run)
        with self.assertRaisesRegex(DailyUseGateError, "session-invalid"):
            self.run_gate()
        self.checkpoint("session-invalid")
        self.assertEqual(self.client.handles, ["original", "pre-existing"])
        self.assertEqual(self.client.selected, "original")
        self.assertNotIn("WebDriver:CloseWindow", self.events)

    def test_contract_valid_failed_function_group_publishes_only_checkpoint(self):
        self.source["functionGroups"][0] = {
            "name": "document",
            "state": "fail",
            "reason": "fixture-capability-failed",
        }
        with self.assertRaisesRegex(DailyUseGateError, "phase-failed"):
            self.run_gate()
        self.checkpoint("phase-failed")

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
                        self.operations = replace(
                            self.operations,
                            fixture=lambda request: FixtureCapture(
                                mutation(
                                    [self.source[field][i] for i in (0, 1, 2, 3, 5)]
                                ),
                                b"fixture",
                            ),
                        )
                    else:
                        self.operations = replace(
                            self.operations,
                            fixture=self.fixture,
                            local_timing=lambda request: TimingCapture(
                                mutation(self.source["performance"][:4]),
                                b"timing",
                                self.source["functionGroups"][4],
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

    def test_no_phase_can_supply_another_phases_functional_verdict(self):
        cases = (
            {
                "fixture": lambda request: FixtureCapture(
                    self.source["functionGroups"], b"fixture"
                )
            },
            {
                "local_timing": lambda request: TimingCapture(
                    self.source["performance"][:4],
                    b"timing",
                    self.source["functionGroups"][6],
                )
            },
            {
                "context_switch": lambda request: ContextCapture(
                    self.source["performance"][4],
                    b"context",
                    self.source["functionGroups"][4],
                )
            },
        )
        for overrides in cases:
            with tempfile.TemporaryDirectory() as directory:
                operations = replace(self.operations, **overrides)
                with self.assertRaisesRegex(DailyUseGateError, "contract-invalid"):
                    self.run_gate(operations=operations, evidence_dir=Path(directory))
                self.assertFalse((Path(directory) / gate.RESULT_NAME).exists())

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
        self.assertEqual(
            checkpoint["functionGroups"], [expected[i] for i in (0, 1, 2, 3, 5)]
        )
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


class AdapterMarionette(FakeMarionette):
    def __init__(self, download):
        super().__init__([])
        self.timing = TimingMarionette()
        self.composite = CompositeMarionette()
        self.download = download
        self.urls = []
        self.capabilities = {
            "version": 1,
            "phase": "home",
            "state": "complete",
            "error": None,
            "checks": {
                name: True
                for name in (
                    "audio",
                    "canvas",
                    "cookie",
                    "fetch",
                    "indexedDb",
                    "localStorage",
                    "sessionStorage",
                    "wasm",
                    "worker",
                )
            },
        }
        self.body = "Asterinas browser quality 浏览器质量"
        self.foreign_resource = False
        self.form_submissions = []
        self.form_result = "fixture-search-scheduled"
        self.form_destination = None

    def command(self, name, parameters=None):
        if name == "WebDriver:Navigate":
            self.urls.append(parameters["url"])
            self.composite.command(name, parameters)
            return self.timing.command(name, parameters)
        if name == "WebDriver:ExecuteScript":
            script = parameters["script"]
            if script == web._FIXTURE_SUBMIT_SCRIPT:
                self.form_submissions.append(self.timing.url)
                self.timing.url = (
                    self.form_destination or self.timing.url + "?q=asterinas"
                )
                return {"value": self.form_result}
            if "CompositeWorkload" in script:
                return self.composite.command(name, parameters)
            if "quality-download" in script:
                self.download.write_bytes(bytes(range(256)) * 1024)
                return {"value": "fixture-download-scheduled"}
            if script in (web._PROBE_SCRIPT, web._SNAPSHOT_SCRIPT):
                url = self.timing.url
                snapshot = {
                    "url": url,
                    "title": "asterinas - Asterinas Browser Quality"
                    if "?q=asterinas" in url
                    else "Asterinas Browser Quality",
                    "readyState": "complete",
                    "bodyText": self.body,
                    "jsComplete": True,
                    "browserCapabilities": {
                        **self.capabilities,
                        "phase": "search" if "?q=asterinas" in url else "home",
                    },
                    "dom": {key: key.startswith("fixture") for key in web._DOM_FIELDS},
                }
                if script == web._SNAPSHOT_SCRIPT:
                    snapshot.update(
                        {
                            "links": [],
                            "navigation": {"name": url, "entryType": "navigation"},
                            "resources": [
                                {
                                    "name": "https://example.com/private"
                                    if self.foreign_resource
                                    else url.rsplit("/", 1)[0] + "/pattern.png",
                                    "initiatorType": "img",
                                    "duration": 2,
                                    "transferSize": 100,
                                }
                            ],
                        }
                    )
                return {"value": json.dumps(snapshot)}
            return self.timing.command(name, parameters)
        return super().command(name, parameters)


class DailyUseAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.download = self.root / "asterinas-browser-quality.bin"
        self.client = AdapterMarionette(self.download)
        self.timeline = (
            "A_WEB_TIMELINE marker=BOOT_FIREFOX_EXEC "
            "guest_monotonic_ns=1000000000 firefox_pid=101\n"
            "A_WEB_TIMELINE marker=BOOT_FIRST_WINDOW_READY "
            "guest_monotonic_ns=1500000000 firefox_pid=101\n"
        )

    def operations(self, **kwargs):
        return gate.default_operations(
            timeline_path=self.root / "timeline",
            timeline_reader=lambda path: self.timeline,
            download_path=self.download,
            firefox_uid_reader=lambda pid: os.geteuid(),
            **kwargs,
        )

    def request(self, **kwargs):
        values = dict(
            client=gate.ExistingSession(self.client),
            original_window="original",
            fixture_index_url=BASE,
            mode="smoke",
            firefox_pid=101,
            xorg_pid=202,
            deadline=time.monotonic() + 10,
            clock=gate.DailyUseClock(),
            run_id=RUN_ID,
            physical=False,
        )
        values.update(kwargs)
        return gate.CaptureRequest(**values)

    def test_factory_provides_typed_operations(self):
        self.assertTrue(callable(getattr(gate, "default_operations", None)))
        self.assertIsInstance(self.operations(), DailyUseOperations)

    def test_fixture_reuses_local_helpers_and_strict_download_for_both_hosts(self):
        for host in ("10.0.2.2", "10.100.19.216"):
            with self.subTest(host=host):
                if self.download.exists():
                    self.download.unlink()
                url = f"http://{host}:17894/browser-quality/index.html"
                with mock.patch.object(
                    web,
                    "_wait_for_fixture_download",
                    wraps=web._wait_for_fixture_download,
                ) as verify:
                    capture = self.operations().fixture(
                        self.request(fixture_index_url=url)
                    )
                self.assertEqual(
                    [item["name"] for item in capture.function_groups],
                    ["document", "storage", "execution", "rendering-media", "download"],
                )
                report = json.loads(capture.artifact)
                self.assertEqual(
                    report["download"]["sha256"], web.FIXTURE_DOWNLOAD_SHA256
                )
                self.assertEqual(report["snapshot"]["url"], url)
                self.assertEqual(verify.call_args.args[0], self.download)
                self.assertEqual(verify.call_args.args[3], os.geteuid())
                self.assertFalse(verify.call_args.args[2].exists())
        self.assertNotIn("WebDriver:NewSession", self.client.events)
        self.assertNotIn("WebDriver:DeleteSession", self.client.events)
        self.assertTrue(all(url.startswith("http://10.") for url in self.client.urls))

    def test_fixture_rejects_foreign_origin_stale_download_and_wrong_owner(self):
        with self.assertRaises((ValueError, DailyUseGateError)):
            self.operations().fixture(
                self.request(fixture_index_url="https://example.com")
            )
        self.assertEqual(self.client.urls, [])
        self.download.write_bytes(b"stale")
        with self.assertRaises(ValueError):
            self.operations().fixture(self.request())
        self.assertEqual(self.download.read_bytes(), b"stale")
        self.download.unlink()
        operations = gate.default_operations(
            download_path=self.download, firefox_uid_reader=lambda pid: os.geteuid() + 1
        )
        with self.assertRaises(web.GateError):
            operations.fixture(self.request())

    def test_fixture_rejects_each_missing_capability_and_foreign_resource(self):
        operations = self.operations()
        for capability in self.client.capabilities["checks"]:
            with self.subTest(capability=capability):
                self.client.capabilities["checks"][capability] = False
                with mock.patch.object(
                    web,
                    "_wait_for_probe",
                    side_effect=lambda client, validator, deadline, **kwargs: (
                        web._probe(client),
                        validator(web._probe(client)),
                    ),
                ):
                    with self.assertRaises(web.GateError):
                        operations.fixture(self.request())
                self.client.capabilities["checks"][capability] = True
        self.client.foreign_resource = True
        with self.assertRaises(ValueError):
            operations.fixture(self.request())

    def test_fixture_rejects_missing_latin_cjk_and_incorrect_download_bytes(self):
        for body in ("Asterinas browser quality", "浏览器质量"):
            self.client.body = body
            with mock.patch.object(
                web,
                "_wait_for_probe",
                side_effect=lambda client, validator, deadline, **kwargs: (
                    web._probe(client),
                    validator(web._probe(client)),
                ),
            ):
                with self.assertRaises(web.GateError):
                    self.operations().fixture(self.request())
        self.client.body = "Asterinas browser quality 浏览器质量"
        from tools.riscv.debian.rootfs import browser_perf_capture as perf

        def corrupt(*args):
            self.download.write_bytes(b"x" * web.FIXTURE_DOWNLOAD_BYTES)
            return "fixture-download-scheduled"

        with mock.patch.object(perf, "_script", side_effect=corrupt):
            with self.assertRaisesRegex(web.GateError, "hash does not match"):
                self.operations().fixture(self.request())

    def test_startup_uses_persisted_first_window_ready_endpoint(self):
        capture = self.operations().local_timing(self.request())
        metrics = capture.performance[0]["metrics"]
        self.assertEqual(metrics["bootFirefoxExecNs"], 1_000_000_000)
        self.assertEqual(metrics["bootFirstWindowReadyNs"], 1_500_000_000)
        self.assertEqual(metrics["durationMs"], 500.0)

    def test_timing_uses_existing_capture_and_preserves_negative_fetch_start(self):
        from tools.riscv.debian.rootfs import browser_perf_capture as perf

        with (
            mock.patch.dict(NAVIGATION, {"fetchStart": -12}),
            mock.patch.object(
                perf, "capture_local", wraps=perf.capture_local
            ) as capture_local,
        ):
            capture = self.operations().local_timing(self.request())
        self.assertEqual(
            capture.function_group,
            {"name": "navigation", "state": "pass", "reason": None},
        )
        self.assertIsInstance(capture_local.call_args.args[0], gate.ExistingSession)
        self.assertEqual(capture_local.call_args.args[1], BASE)
        performance = capture.performance
        self.assertEqual(
            [entry["name"] for entry in performance],
            ["startup", "input", "scroll", "navigation"],
        )
        self.assertEqual(performance[0]["metrics"]["durationMs"], 500)
        self.assertEqual(
            performance[1]["metrics"]["keyboard"]["firstRaf"], {"p50Ms": 2, "p95Ms": 2}
        )
        self.assertEqual(performance[2]["metrics"]["nextRaf"], {"p50Ms": 4, "p95Ms": 4})
        navigation = performance[3]["metrics"]["browserNavigation"]
        self.assertEqual(navigation["fetchStartMs"], -12)
        self.assertIs(navigation["fetchStartValid"], False)
        self.assertEqual(navigation["responseToDomMs"], 10)
        self.assertEqual(navigation["responseToLoadMs"], 20)
        self.assertEqual(
            json.loads(capture.artifact)["navigation_snapshot"]["fetchStart"], -12
        )

    def test_navigation_verdict_requires_fixture_form_submission_on_both_hosts(self):
        for host in ("10.0.2.2", "10.100.19.216"):
            url = f"http://{host}:17894/browser-quality/index.html"
            result = self.operations().local_timing(self.request(fixture_index_url=url))
            self.assertIn(url, self.client.form_submissions)
            evidence = json.loads(result.artifact)["form_navigation"]
            self.assertEqual(evidence["snapshot"]["url"], url + "?q=asterinas")
            self.assertEqual(result.function_group["state"], "pass")

    def test_broken_form_submission_or_destination_cannot_pass_navigation(self):
        from tools.riscv.debian.rootfs import browser_perf_capture as perf

        for failure in ("submit", "destination"):
            self.client.form_result = (
                "missing-controls"
                if failure == "submit"
                else "fixture-search-scheduled"
            )
            self.client.form_destination = (
                "https://example.invalid/" if failure == "destination" else None
            )
            with (
                mock.patch.object(
                    web,
                    "_wait_for_probe",
                    side_effect=lambda client, validator, deadline, **kwargs: (
                        web._probe(client),
                        validator(web._probe(client)),
                    ),
                ),
                mock.patch.object(
                    perf, "capture_local", wraps=perf.capture_local
                ) as capture,
            ):
                with self.assertRaises((web.GateError, DailyUseGateError)):
                    self.operations().local_timing(self.request())
            capture.assert_not_called()

    def test_cli_real_adapters_emit_only_the_terminal_verdict(self):
        def sample(request):
            start = time.monotonic_ns()
            request.ready.set()
            request.stop.wait(2)
            return SamplerCapture(b"sample", start, time.monotonic_ns())

        exec_ns = time.monotonic_ns() - 500_000_000
        self.timeline = (
            f"A_WEB_TIMELINE marker=BOOT_FIREFOX_EXEC guest_monotonic_ns={exec_ns} firefox_pid=101\n"
            f"A_WEB_TIMELINE marker=BOOT_FIRST_WINDOW_READY guest_monotonic_ns={exec_ns + 400_000_000} firefox_pid=101\n"
        )
        for fail in (False, True):
            if self.download.exists():
                self.download.unlink()
            self.client = AdapterMarionette(self.download)
            self.client.foreign_resource = fail
            with tempfile.TemporaryDirectory() as directory:
                operations = replace(
                    self.operations(),
                    identity_reader=lambda pids: (1000, 2000),
                    system_sampler=sample,
                    thread_sampler=sample,
                )
                with (
                    mock.patch.object(
                        gate, "default_operations", return_value=operations
                    ),
                    mock.patch.object(gate, "_connect", return_value=self.client),
                    mock.patch.object(gate.secrets, "token_hex", return_value=RUN_ID),
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out,
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err,
                ):
                    code = gate.main(
                        [
                            "--firefox-pid",
                            "101",
                            "--xorg-pid",
                            "202",
                            "--fixture-index-url",
                            BASE,
                            "--evidence-dir",
                            directory,
                        ]
                    )
                self.assertEqual(code, int(fail))
                self.assertEqual(
                    out.getvalue(),
                    ""
                    if fail
                    else f"ASTERINAS_BROWSER_DAILY_USE_PASS functions=7/7 slow=0 evidence_dir={directory}\n",
                )
                self.assertEqual(
                    err.getvalue(),
                    "ASTERINAS_BROWSER_DAILY_USE_FAIL reason=phase-value-invalid\n"
                    if fail
                    else "",
                )

    def test_form_destination_requires_text_dom_and_complete_search_capabilities(self):
        original_probe = web._probe

        def observed_with(mutate):
            def observed(client):
                value = original_probe(client)
                if value["url"].endswith("?q=asterinas"):
                    mutate(value)
                return value

            return observed

        mutations = (
            lambda value: value.update(bodyText="Asterinas browser quality"),
            lambda value: value.update(title="Asterinas Browser Quality"),
            lambda value: value["dom"].update(fixtureQuery=False),
            lambda value: value["dom"].update(fixtureImage=False),
            lambda value: value["dom"].update(fixtureSecond=False),
            lambda value: value["browserCapabilities"].update(state="running"),
            lambda value: value["browserCapabilities"].update(phase="home"),
        )
        for mutation in mutations:
            with (
                mock.patch.object(web, "_probe", side_effect=observed_with(mutation)),
                mock.patch.object(
                    web,
                    "_wait_for_probe",
                    side_effect=lambda client, validator, deadline, **kwargs: (
                        web._probe(client),
                        validator(web._probe(client)),
                    ),
                ),
            ):
                with self.assertRaises(web.GateError):
                    self.operations().local_timing(self.request())

    def test_unavailable_response_intervals_are_supported_unsupported_evidence(self):
        for response in (None, 0, -1):
            with (
                self.subTest(response=response),
                mock.patch.dict(NAVIGATION, {"responseEnd": response}),
            ):
                capture = self.operations().local_timing(self.request())
            self.assertEqual(
                capture.performance[3],
                {
                    "name": "navigation",
                    "state": "unsupported",
                    "clockDomain": "multiple-clock-domains-separated",
                    "metrics": {},
                    "reason": "navigation-timing-invalid",
                },
            )
            self.assertEqual(capture.function_group["state"], "pass")

    def test_startup_rejects_duplicates_order_pid_and_clock_errors(self):
        original = self.timeline
        exec_record, ready_record = original.splitlines(keepends=True)
        invalid = (
            exec_record,
            original + ready_record,
            exec_record + ready_record.replace("firefox_pid=101", "firefox_pid=102"),
            ready_record + exec_record,
            exec_record + ready_record.replace("guest_monotonic_ns=1500000000", "guest_monotonic_ns=0"),
            exec_record
            + ready_record.replace(
                "guest_monotonic_ns=1500000000", "guest_monotonic_ns=500000000"
            ),
            exec_record.replace("firefox_pid=101", "firefox_pid=102") + ready_record,
            "",
        )
        for self.timeline in invalid:
            with self.subTest(timeline=self.timeline), self.assertRaises(ValueError):
                self.operations().local_timing(self.request())
        self.timeline = (
            original
            + "A_WEB_TIMELINE marker=BOOT_MARIONETTE_CONNECTED "
            "guest_monotonic_ns=1600000000 firefox_pid=101\n"
        )
        self.assertEqual(
            self.operations()
            .local_timing(self.request())
            .performance[0]["metrics"]["durationMs"],
            500,
        )

    def test_context_measures_four_operations_and_cleans_up_on_success_and_failure(
        self,
    ):
        capture = self.operations().context_switch(self.request())
        self.assertEqual(capture.function_group["name"], "contexts")
        metrics = capture.performance["metrics"]
        self.assertEqual(
            metrics["totalMs"],
            sum(metrics[key] for key in ("openMs", "selectMs", "returnMs", "closeMs")),
        )
        self.assertEqual(
            (metrics["handleCountBefore"], metrics["handleCountAfter"]), (1, 1)
        )
        self.assertEqual(self.client.handles, ["original"])
        self.assertEqual(self.client.selected, "original")
        with mock.patch.object(web, "_navigate", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.operations().context_switch(self.request())
        self.assertEqual(self.client.handles, ["original"])
        self.assertEqual(self.client.selected, "original")

    def test_context_deadline_expiry_still_attempts_finally_cleanup(self):
        now = [1.0]
        clock = gate.DailyUseClock(
            monotonic=lambda: now[0], monotonic_ns=lambda: int(now[0] * 1e9)
        )

        def expire(*args):
            now[0] = 3.0
            raise TimeoutError("navigation expired")

        with mock.patch.object(web, "_navigate", side_effect=expire):
            with self.assertRaises((TimeoutError, DailyUseGateError)):
                self.operations().context_switch(
                    self.request(clock=clock, deadline=2.0)
                )
        self.assertEqual(self.client.handles, ["original"])
        self.assertEqual(self.client.selected, "original")

    def test_existing_session_skips_drain_for_a_command_that_never_sent(self):
        from tools.riscv.debian.rootfs import browser_m5_marionette_gate as wire
        from tools.riscv.tests.test_debian_browser_m5_runtime_gate import (
            _Socket,
            _frame,
        )

        transport = _Socket(
            _frame({"applicationType": "gecko", "marionetteProtocol": 3})
        )
        now = [1.0]
        with (
            mock.patch.object(wire.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(wire.socket, "create_connection", return_value=transport),
        ):
            client = wire.Marionette("127.0.0.1", 2828, 1)
            session = gate.ExistingSession(client)
            now[0] = 3.0
            with self.assertRaises(TimeoutError):
                session.command("WebDriver:NewWindow")
            self.assertEqual(bytes(transport.sent), b"")
            with mock.patch.object(
                client,
                "recover_timed_out_command",
                wraps=client.recover_timed_out_command,
            ) as drain:
                session.recover_timed_out_command(1)
            drain.assert_not_called()
            transport.incoming.extend(_frame([1, 1, None, {"value": ["original"]}]))
            self.assertEqual(
                session.command("WebDriver:GetWindowHandles"), {"value": ["original"]}
            )

    def test_context_recovers_late_socket_response_before_closing_second_tab(self):
        from tools.riscv.debian.rootfs import browser_m5_marionette_gate as transport

        for delayed_command in ("WebDriver:NewWindow", "WebDriver:Navigate"):
            for partial_header in (False, True):
                with self.subTest(
                    command=delayed_command, partial_header=partial_header
                ):
                    browser = AdapterMarionette(self.download)
                    local, peer = socket.socketpair()
                    release = threading.Event()
                    commands, errors = [], []

                    def frame(value):
                        payload = json.dumps(value).encode()
                        return str(len(payload)).encode() + b":" + payload

                    def server():
                        try:
                            peer.sendall(
                                frame(
                                    {
                                        "applicationType": "gecko",
                                        "marionetteProtocol": 3,
                                    }
                                )
                            )
                            while True:
                                header = bytearray()
                                while True:
                                    byte = peer.recv(1)
                                    if not byte:
                                        return
                                    if byte == b":":
                                        break
                                    header.extend(byte)
                                payload = bytearray()
                                while len(payload) < int(header):
                                    chunk = peer.recv(int(header) - len(payload))
                                    if not chunk:
                                        return
                                    payload.extend(chunk)
                                kind, identifier, name, parameters = json.loads(payload)
                                self.assertEqual(kind, 0)
                                commands.append(name)
                                value = browser.command(name, parameters)
                                response = frame([1, identifier, None, value])
                                if name == delayed_command:
                                    cut = (
                                        1
                                        if partial_header
                                        else response.index(b":") + 6
                                    )
                                    peer.sendall(response[:cut])
                                    if not release.wait(2):
                                        raise RuntimeError(
                                            "recovery did not release the late response"
                                        )
                                    peer.sendall(response[cut:])
                                else:
                                    peer.sendall(response)
                        except OSError:
                            # The failing red case closes its unrecovered socket.
                            if not release.is_set():
                                errors.append("unexpected socket failure")
                        except BaseException as error:
                            errors.append(error)
                        finally:
                            peer.close()

                    worker = threading.Thread(target=server, daemon=True)
                    worker.start()
                    with mock.patch.object(
                        transport.socket, "create_connection", return_value=local
                    ):
                        client = transport.Marionette("127.0.0.1", 2828, 1)
                    original_recover = getattr(
                        client, "recover_timed_out_command", None
                    )

                    def recover(timeout):
                        release.set()
                        self.assertIsNotNone(original_recover)
                        return original_recover(timeout)

                    try:
                        client.set_timeout(0.03)
                        with (
                            mock.patch.object(
                                client,
                                "recover_timed_out_command",
                                side_effect=recover,
                                create=True,
                            ),
                            self.assertRaises(TimeoutError),
                        ):
                            self.operations().context_switch(
                                self.request(client=gate.ExistingSession(client))
                            )
                        self.assertEqual(browser.handles, ["original"])
                        self.assertEqual(browser.selected, "original")
                        self.assertEqual(commands.count(delayed_command), 1)
                        self.assertNotIn("WebDriver:NewSession", commands)
                        self.assertNotIn("WebDriver:DeleteSession", commands)
                        self.assertIn("WebDriver:CloseWindow", commands)
                    finally:
                        release.set()
                        client.close()
                        worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])

    def test_factory_runs_complete_workload_with_one_session_and_hashed_artifacts(self):
        from tools.riscv.debian.rootfs import browser_system_time as system

        exec_ns = time.monotonic_ns() - 500_000_000
        self.timeline = (
            f"A_WEB_TIMELINE marker=BOOT_FIREFOX_EXEC guest_monotonic_ns={exec_ns} firefox_pid=101\n"
            f"A_WEB_TIMELINE marker=BOOT_FIRST_WINDOW_READY guest_monotonic_ns={exec_ns + 400_000_000} firefox_pid=101\n"
        )

        def sample(*args, **kwargs):
            start = kwargs["clock_ns"]()
            kwargs["ready_fn"]()
            self.assertTrue(kwargs["stop_event"].wait(2))
            end = kwargs["clock_ns"]()
            args[-1].write_text(
                json.dumps(
                    {
                        "intervals": [
                            {
                                "guest_monotonic_start_ns": start,
                                "guest_monotonic_end_ns": end,
                            }
                        ]
                    }
                )
            )

        operations = replace(
            self.operations(), identity_reader=lambda pids: (1000, 2000)
        )
        with (
            mock.patch.object(system, "run_sampler", side_effect=sample),
            mock.patch.object(system, "run_thread_sampler", side_effect=sample),
        ):
            result = gate.run_daily_use_gate(
                client=self.client,
                operations=operations,
                firefox_pid=101,
                xorg_pid=202,
                evidence_dir=self.root,
                mode="smoke",
                timeout_seconds=10,
                run_id=RUN_ID,
                fixture_index_url=BASE,
            )
        self.assertEqual(result["state"], "pass")
        self.assertEqual(len(result["functionGroups"]), 7)
        self.assertEqual(self.client.events.count("WebDriver:NewSession"), 1)
        self.assertNotIn("WebDriver:DeleteSession", self.client.events)
        self.assertEqual(self.client.handles, ["original"])
        self.assertGreater(result["performance"][0]["metrics"]["durationMs"], 0)
        composite = json.loads((self.root / gate.ARTIFACT_NAMES[3]).read_text())
        self.assertEqual(len(composite["phase_observations"]), 7)
        self.assertEqual(composite["run_id"], RUN_ID)
        for artifact in result["artifacts"]:
            payload = (self.root / artifact["name"]).read_bytes()
            self.assertEqual(artifact["sha256"], hashlib.sha256(payload).hexdigest())

    def test_cli_invalid_inputs_produce_only_one_canonical_failure(self):
        args = [
            "--firefox-pid",
            "101",
            "--xorg-pid",
            "202",
            "--fixture-index-url",
            BASE,
            "--evidence-dir",
            str(self.root),
        ]
        for extra in (
            ["--port", "0"],
            ["--timeout-seconds", "nan"],
            ["--mode", "stress"],
            ["--evidence-dir", str(self.root / "line\nbreak")],
        ):
            with (
                self.subTest(extra=extra),
                mock.patch.object(gate, "_connect") as connect,
                mock.patch("sys.stdout", new_callable=io.StringIO) as out,
                mock.patch("sys.stderr", new_callable=io.StringIO) as err,
            ):
                self.assertEqual(gate.main(args + extra), 1)
            connect.assert_not_called()
            self.assertEqual(out.getvalue(), "")
            self.assertRegex(
                err.getvalue(), r"\AASTERINAS_BROWSER_DAILY_USE_FAIL reason=[a-z-]+\n\Z"
            )

    def test_composite_reuses_existing_session_run_identity_and_remaining_budget(self):
        from tools.riscv.debian.rootfs import browser_composite_capture as composite

        report = {"run_id": RUN_ID, "phase_observations": ["seven phases"]}
        request = self.request(mode="profile", physical=True)
        with mock.patch.object(
            composite, "capture_composite", return_value=report
        ) as capture:
            result = self.operations().composite(request)
        self.assertEqual(json.loads(result.artifact), report)
        self.assertEqual(capture.call_args.args, (request.client, BASE))
        self.assertEqual(capture.call_args.kwargs["run_id"], RUN_ID)
        self.assertEqual(capture.call_args.kwargs["mode"], "profile")
        self.assertTrue(0 < capture.call_args.kwargs["timeout_seconds"] <= 10)
        self.assertIn("physical-scanout-unsupported", result.limitations["items"])

    def test_sampler_adapters_bridge_events_private_outputs_and_coverage(self):
        from tools.riscv.debian.rootfs import browser_system_time as system

        for name, runner in (
            ("system_sampler", "run_sampler"),
            ("thread_sampler", "run_thread_sampler"),
        ):
            request = gate.SamplerRequest(
                (101, 202),
                "profile",
                threading.Event(),
                threading.Event(),
                time.monotonic() + 30,
                gate.DailyUseClock(),
                physical=True,
            )
            paths = []

            def sample(*args, **kwargs):
                paths.append(args[-1])
                self.assertEqual(args[0], Path("/proc"))
                self.assertFalse(args[-1].exists())
                self.assertEqual(args[-1].parent.stat().st_mode & 0o777, 0o700)
                if runner == "run_thread_sampler":
                    self.assertTrue(kwargs["physical"])
                    self.assertTrue(args[2].read_text().strip().isdigit())
                kwargs["ready_fn"]()
                self.assertTrue(request.ready.is_set())
                self.assertIs(kwargs["stop_event"], request.stop)
                request.stop.set()
                args[-1].write_text(
                    json.dumps(
                        {
                            "intervals": [
                                {
                                    "guest_monotonic_start_ns": 100,
                                    "guest_monotonic_end_ns": 200,
                                }
                            ]
                        }
                    )
                )
                return {"ignored": "read authoritative artifact"}

            with mock.patch.object(system, runner, side_effect=sample):
                result = getattr(self.operations(), name)(request)
            self.assertEqual(
                (result.first_sample_ns, result.last_sample_ns), (100, 200)
            )
            self.assertIn("intervals", json.loads(result.artifact))
            self.assertFalse(paths[0].parent.exists())

    def test_cli_emits_exact_pass_and_fail_lines_and_defaults_physical_to_profile(self):
        for physical, mode in ((False, "smoke"), (True, "profile")):
            args = [
                "--firefox-pid",
                "101",
                "--xorg-pid",
                "202",
                "--fixture-index-url",
                BASE,
                "--evidence-dir",
                str(self.root),
                "--timeout-seconds",
                "10",
            ]
            if physical:
                args.append("--physical")
            with (
                mock.patch.object(
                    gate, "_connect", return_value=self.client
                ) as connect,
                mock.patch.object(
                    gate, "run_daily_use_gate", return_value=complete_result()
                ) as run,
                mock.patch("sys.stdout", new_callable=io.StringIO) as out,
                mock.patch("sys.stderr", new_callable=io.StringIO) as err,
            ):
                self.assertEqual(gate.main(args), 0)
            self.assertEqual(
                out.getvalue(),
                f"ASTERINAS_BROWSER_DAILY_USE_PASS functions=7/7 slow=0 evidence_dir={self.root}\n",
            )
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(connect.call_args.args[:2], ("127.0.0.1", 2828))
            self.assertEqual(run.call_args.kwargs["mode"], mode)
            self.assertEqual(run.call_args.kwargs["physical"], physical)
        with (
            mock.patch.object(gate, "_connect", side_effect=OSError("secret")),
            mock.patch("sys.stdout", new_callable=io.StringIO) as out,
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.assertEqual(gate.main(args), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(
            err.getvalue(), "ASTERINAS_BROWSER_DAILY_USE_FAIL reason=phase-failed\n"
        )

    def test_cli_gives_physical_marionette_connect_a_cold_start_budget(self):
        operations = mock.Mock(
            clock=gate.DailyUseClock(monotonic=lambda: 1000.0)
        )
        args = [
            "--firefox-pid",
            "101",
            "--xorg-pid",
            "202",
            "--fixture-index-url",
            BASE,
            "--evidence-dir",
            str(self.root),
            "--timeout-seconds",
            "10",
            "--physical",
        ]
        with (
            mock.patch.object(gate, "default_operations", return_value=operations),
            mock.patch.object(gate, "_connect", return_value=self.client) as connect,
            mock.patch.object(
                gate, "run_daily_use_gate", return_value=complete_result()
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(gate.main(args), 0)
        self.assertEqual(connect.call_args.args[2], 1300.0)


if __name__ == "__main__":
    unittest.main()
