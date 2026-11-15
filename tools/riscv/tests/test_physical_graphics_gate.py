#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the deterministic physical graphics interaction witness."""

from __future__ import annotations

import importlib
import base64
import hashlib
import io
from pathlib import Path
import selectors
import struct
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zlib


ROOTFS_DIRECTORY = Path(__file__).parents[1] / "debian" / "rootfs"
PAGE = ROOTFS_DIRECTORY / "physical_graphics_interaction.html"
GATE = ROOTFS_DIRECTORY / "physical_graphics_gate.py"


def png_payload(*, width: int = 1920, height: int = 1080, value: int = 0x35) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", checksum)
        )

    row = b"\0" + bytes((value, 0x77, 0xB5)) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def load_gate(test: unittest.TestCase):
    test.assertTrue(GATE.is_file(), "physical graphics guest gate is missing")
    name = "tools.riscv.debian.rootfs.physical_graphics_gate"
    sys.modules.pop(name, None)
    return importlib.import_module(name)


class PhysicalGraphicsPageTests(unittest.TestCase):
    def _page(self) -> str:
        self.assertTrue(PAGE.is_file(), "physical graphics page is missing")
        return PAGE.read_text(encoding="utf-8")

    def test_page_exposes_the_exact_interaction_surface(self) -> None:
        page = self._page()
        for element_id in (
            "interaction-nonce",
            "interaction-button",
            "interaction-state",
            "interaction-instructions",
        ):
            self.assertIn(f'id="{element_id}"', page)
        self.assertIn("window.__asterinasPhysicalGraphicsSnapshot", page)

    def test_page_records_only_trusted_physical_events(self) -> None:
        page = self._page()
        for event_name in ("keydown", "input", "pointermove", "click"):
            self.assertIn(f'addEventListener("{event_name}"', page)
        self.assertGreaterEqual(page.count("event.isTrusted"), 4)
        self.assertIn("button.disabled = true", page)
        self.assertIn("document.title", page)
        self.assertIn("connect-src 'self'", page)
        self.assertIn("fetch(`/stage?", page)
        self.assertIn("button.getBoundingClientRect()", page)
        self.assertIn("keyComplete()", page)
        self.assertNotIn("event.movementX", page)
        self.assertNotIn("event.movementY", page)
        for stage in ("waiting", "key", "pointer", "complete"):
            self.assertIn(f'"{stage}"', page)

    def test_page_is_self_contained_and_uses_the_frozen_state_names(self) -> None:
        page = self._page()
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link rel=", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        for state in (
            "trustedKey: false",
            "trustedInput: false",
            "trustedPointer: false",
            "trustedClick: false",
            "clickCount: 0",
            'color: "amber"',
        ):
            self.assertIn(state, page)


class EvdevCycleTests(unittest.TestCase):
    def test_real_source_returns_every_complete_record(self) -> None:
        gate = load_gate(self)
        first = gate.INPUT_EVENT_STRUCT.pack(1, 2, gate.EV_KEY, 30, 1)
        second = gate.INPUT_EVENT_STRUCT.pack(1, 3, gate.EV_REL, gate.REL_X, 4)
        source = object.__new__(gate.RealEvdevSource)
        source._selector = mock.Mock()
        key = mock.Mock(fd=7)
        source._selector.select.return_value = [(key, selectors.EVENT_READ)]
        source._buffers = {7: bytearray()}

        with mock.patch.object(gate.os, "read", return_value=first + second):
            self.assertEqual(source.poll(0.0), [first, second])

    def test_counts_keyboard_motion_and_one_ordered_left_click(self) -> None:
        gate = load_gate(self)
        cycle = gate.EvdevCycle()
        cycle.feed(gate.InputEvent(0, 0, gate.EV_KEY, 30, 1))
        cycle.feed(gate.InputEvent(0, 0, gate.EV_REL, gate.REL_X, 4))
        cycle.feed(gate.InputEvent(0, 0, gate.EV_KEY, gate.BTN_LEFT, 1))
        cycle.feed(gate.InputEvent(0, 0, gate.EV_KEY, gate.BTN_LEFT, 0))
        self.assertEqual(cycle.key_downs, 1)
        self.assertEqual(cycle.relative_events, 1)
        self.assertEqual(cycle.absolute_events, 0)
        self.assertTrue(cycle.left_click_complete)

    def test_counts_qemu_tablet_absolute_axis_events_separately(self) -> None:
        gate = load_gate(self)
        cycle = gate.EvdevCycle()
        cycle.feed(gate.InputEvent(0, 0, gate.EV_ABS, gate.ABS_X, 32767))
        cycle.feed(gate.InputEvent(0, 1, gate.EV_ABS, gate.ABS_Y, 16384))
        self.assertEqual(cycle.relative_events, 0)
        self.assertEqual(cycle.absolute_events, 2)

    def test_feed_record_requires_one_exact_native_riscv64_record(self) -> None:
        gate = load_gate(self)
        payload = gate.INPUT_EVENT_STRUCT.pack(1, 2, gate.EV_KEY, 30, 1)
        cycle = gate.EvdevCycle()
        cycle.feed_record(payload)
        self.assertEqual(cycle.key_downs, 1)
        with self.assertRaisesRegex(gate.GateError, "truncated"):
            cycle.feed_record(payload[:-1])

    def test_rejects_button_up_before_down_and_counter_overflow(self) -> None:
        gate = load_gate(self)
        cycle = gate.EvdevCycle()
        with self.assertRaisesRegex(gate.GateError, "button-up-before-down"):
            cycle.feed(gate.InputEvent(0, 0, gate.EV_KEY, gate.BTN_LEFT, 0))
        cycle = gate.EvdevCycle(key_downs=gate.MAX_EVENT_COUNT)
        with self.assertRaisesRegex(gate.GateError, "counter-overflow"):
            cycle.feed(gate.InputEvent(0, 0, gate.EV_KEY, 30, 1))


class PhysicalGraphicsSnapshotTests(unittest.TestCase):
    @staticmethod
    def _snapshot(nonce: str = "0123456789abcdef", cycle: int = 1) -> dict[str, object]:
        return {
            "cycle": cycle,
            "nonce": nonce,
            "trustedKey": True,
            "trustedInput": True,
            "trustedPointer": True,
            "trustedClick": True,
            "clickCount": 1,
            "color": "cyan",
        }

    def test_accepts_only_the_exact_completed_snapshot(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "validate_snapshot"), "snapshot validator is missing"
        )
        expected = self._snapshot()
        gate.validate_snapshot(expected, expected_nonce="0123456789abcdef", cycle=1)
        for name, value in (
            ("cycle", 2),
            ("nonce", "fedcba9876543210"),
            ("trustedKey", False),
            ("trustedInput", False),
            ("trustedPointer", False),
            ("trustedClick", False),
            ("clickCount", 2),
            ("color", "amber"),
        ):
            with self.subTest(name=name):
                mutated = {**expected, name: value}
                with self.assertRaises(gate.GateError):
                    gate.validate_snapshot(
                        mutated, expected_nonce="0123456789abcdef", cycle=1
                    )

    def test_rejects_unknown_missing_and_non_boolean_fields(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "validate_snapshot"), "snapshot validator is missing"
        )
        expected = self._snapshot()
        for mutated in (
            {**expected, "unknown": 1},
            {name: value for name, value in expected.items() if name != "color"},
            {**expected, "trustedKey": 1},
        ):
            with self.assertRaises(gate.GateError):
                gate.validate_snapshot(
                    mutated, expected_nonce="0123456789abcdef", cycle=1
                )

    def test_ready_marionette_allows_only_snapshot_and_screenshot(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "GuardedMarionette"), "guarded Marionette is missing"
        )

        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object | None]] = []

            def command(self, name: str, parameters: object | None = None) -> object:
                self.calls.append((name, parameters))
                return {"value": None}

        client = Client()
        guarded = gate.GuardedMarionette(client)
        guarded.mark_ready()
        guarded.snapshot()
        guarded.screenshot()
        self.assertEqual(
            [name for name, _ in client.calls],
            ["WebDriver:ExecuteScript", "WebDriver:TakeScreenshot"],
        )
        for name in ("WebDriver:PerformActions", "WebDriver:ElementClick"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(gate.GateError, "input-synthesis"),
            ):
                guarded.command(name, {})

    def test_snapshot_reads_serialized_dom_state_in_the_named_sandbox(self) -> None:
        gate = load_gate(self)
        client = mock.Mock()
        guarded = gate.GuardedMarionette(client)
        guarded.mark_ready()
        guarded.snapshot()
        name, parameters = client.command.call_args.args
        self.assertEqual(name, "WebDriver:ExecuteScript")
        self.assertIn("interaction-state", parameters["script"])
        self.assertIn("JSON.parse", parameters["script"])
        self.assertNotIn("__asterinasPhysicalGraphicsSnapshot", parameters["script"])
        self.assertEqual(parameters["args"], [])
        self.assertEqual(parameters["sandbox"], "default")
        self.assertIs(parameters["newSandbox"], True)
        for changed in (
            dict(parameters, script="document.querySelector('button').click();"),
            dict(parameters, args=["0123456789abcdef"]),
            dict(parameters, sandbox="other"),
        ):
            with self.assertRaisesRegex(gate.GateError, "input-synthesis"):
                guarded.command(name, changed)
        client.command.assert_called_once()


class FirefoxNamespaceTests(unittest.TestCase):
    def test_accepts_online_firefox_in_the_same_non_loopback_namespace(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "validate_firefox_namespace"),
            "online Firefox namespace validator is missing",
        )
        with (
            mock.patch.object(
                gate.os,
                "readlink",
                side_effect=("net:[4026532000]", "net:[4026532000]"),
            ),
            mock.patch.object(
                gate.socket, "if_nameindex", return_value=[(1, "lo"), (2, "eth0")]
            ),
        ):
            gate.validate_firefox_namespace(42)

    def test_rejects_invalid_pid_and_a_different_namespace(self) -> None:
        gate = load_gate(self)
        self.assertTrue(hasattr(gate, "validate_firefox_namespace"))
        with self.assertRaises(gate.GateError):
            gate.validate_firefox_namespace(1)
        with (
            mock.patch.object(
                gate.os,
                "readlink",
                side_effect=("net:[4026532000]", "net:[4026532001]"),
            ),
            self.assertRaises(gate.GateError),
        ):
            gate.validate_firefox_namespace(42)

    def test_rejects_loopback_only_and_multiple_non_loopback_interfaces(self) -> None:
        gate = load_gate(self)
        for interfaces in ([(1, "lo")], [(1, "lo"), (2, "eth0"), (3, "eth1")]):
            with (
                self.subTest(interfaces=interfaces),
                mock.patch.object(
                    gate.os,
                    "readlink",
                    side_effect=("net:[4026532000]", "net:[4026532000]"),
                ),
                mock.patch.object(gate.socket, "if_nameindex", return_value=interfaces),
                self.assertRaisesRegex(gate.GateError, "one non-loopback"),
            ):
                gate.validate_firefox_namespace(42)


class X11WindowTitleSourceTests(unittest.TestCase):
    def test_reads_only_the_active_window_owned_by_firefox(self) -> None:
        gate = load_gate(self)
        title = gate.dom_stage_title(nonce="0123456789abcdef", cycle=1, stage="key")
        replies = (
            mock.Mock(returncode=0, stdout="4194305\n", stderr=""),
            mock.Mock(returncode=0, stdout="42\n", stderr=""),
            mock.Mock(returncode=0, stdout=title + "\n", stderr=""),
            mock.Mock(returncode=0, stdout=title + "\n", stderr=""),
        )
        with mock.patch.object(gate.subprocess, "run", side_effect=replies) as run:
            source = gate.X11WindowTitleSource(42)
            self.assertEqual(source.read_title(5), title)
            self.assertEqual(source.read_title(5), title)

        self.assertEqual(run.call_count, 4)
        self.assertEqual(run.call_args_list[0].args[0][-1], "getwindowfocus")
        self.assertEqual(
            run.call_args_list[1].args[0][-2:], ["getwindowpid", "4194305"]
        )
        self.assertEqual(
            run.call_args_list[2].args[0][-2:], ["getwindowname", "4194305"]
        )

    def test_rejects_a_window_not_owned_by_the_selected_firefox(self) -> None:
        gate = load_gate(self)
        replies = (
            mock.Mock(returncode=0, stdout="4194305\n", stderr=""),
            mock.Mock(returncode=0, stdout="43\n", stderr=""),
        )
        with (
            mock.patch.object(gate.subprocess, "run", side_effect=replies),
            self.assertRaisesRegex(gate.GateError, "window-owner"),
        ):
            gate.X11WindowTitleSource(42).read_title(5)

    def test_rejects_multiline_or_unknown_stage_titles(self) -> None:
        gate = load_gate(self)
        replies = (
            mock.Mock(returncode=0, stdout="4194305\n", stderr=""),
            mock.Mock(returncode=0, stdout="42\n", stderr=""),
            mock.Mock(returncode=0, stdout="first\nsecond\n", stderr=""),
        )
        with (
            mock.patch.object(gate.subprocess, "run", side_effect=replies),
            self.assertRaisesRegex(gate.GateError, "title-value"),
        ):
            gate.X11WindowTitleSource(42).read_title(5)
        with self.assertRaisesRegex(gate.GateError, "title-stage"):
            gate.dom_stage_title(nonce="0123456789abcdef", cycle=1, stage="unknown")

    def test_query_failure_identifies_the_operation_exit_status_and_stderr(
        self,
    ) -> None:
        gate = load_gate(self)
        failed = mock.Mock(
            returncode=1,
            stdout="",
            stderr="Your windowmanager claims not to support _NET_ACTIVE_WINDOW\n",
        )
        with (
            mock.patch.object(gate.subprocess, "run", return_value=failed),
            self.assertRaisesRegex(
                gate.GateError,
                r"title-query:getwindowfocus:exit=1:stderr=",
            ),
        ):
            gate.X11WindowTitleSource(42).read_title(5)


class DomStageServerTests(unittest.TestCase):
    def test_serves_the_frozen_page_and_accepts_only_nonce_bound_stages(self) -> None:
        gate = load_gate(self)
        nonce = "0123456789abcdef"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with gate.DomStageServer(nonce=nonce, cycle=2, page_path=PAGE) as server:
            with opener.open(server.page_url, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), PAGE.read_bytes())

            invalid = f"{server.origin}/stage?cycle=2&stage=key&nonce=fedcba9876543210"
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                opener.open(invalid, timeout=2)
            self.assertEqual(rejected.exception.code, 400)

            accepted = f"{server.origin}/stage?cycle=2&stage=key&nonce={nonce}"
            with opener.open(accepted, timeout=2) as response:
                self.assertEqual(response.status, 204)
            self.assertEqual(
                server.read_title(1),
                gate.dom_stage_title(nonce=nonce, cycle=2, stage="key"),
            )

    def test_rejects_a_stage_regression_and_bounds_waiting(self) -> None:
        gate = load_gate(self)
        nonce = "0123456789abcdef"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with gate.DomStageServer(nonce=nonce, cycle=1, page_path=PAGE) as server:
            for stage in ("pointer", "key"):
                target = f"{server.origin}/stage?cycle=1&stage={stage}&nonce={nonce}"
                try:
                    opener.open(target, timeout=2).close()
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 409)
            with self.assertRaisesRegex(gate.GateError, "stage-regression"):
                server.read_title(1)


class ScreenshotValidationTests(unittest.TestCase):
    def test_accepts_only_complete_1920_by_1080_png(self) -> None:
        gate = load_gate(self)
        payload = png_payload()
        gate.validate_png_screenshot(payload)
        variants = (
            png_payload(width=1280, height=1024),
            payload[:-12],
            payload[:-1] + bytes((payload[-1] ^ 1,)),
            b"\x89PNG\r\n\x1a\nnot-a-png",
        )
        for variant in variants:
            with self.subTest(size=len(variant)), self.assertRaises(gate.GateError):
                gate.validate_png_screenshot(variant)


class PhysicalGraphicsRunTests(unittest.TestCase):
    def test_main_normalizes_transport_failure_and_closes_resources(self) -> None:
        gate = load_gate(self)
        transport = importlib.import_module(
            "tools.riscv.debian.rootfs.browser_m5_marionette_gate"
        )
        for phase in ("connect", "setup", "snapshot", "final"):
            with self.subTest(phase=phase):
                client = self.Client(PhysicalGraphicsSnapshotTests._snapshot())
                client.close = mock.Mock()
                records = []
                if phase == "snapshot":
                    records = [
                        GATE_EVENT
                        for GATE_EVENT in (
                            *(
                                struct.pack("=qqHHi", 0, index, 1, 30, 1)
                                for index in range(16)
                            ),
                            struct.pack("=qqHHi", 0, 20, 2, 0, 1),
                            struct.pack("=qqHHi", 0, 21, 1, 0x110, 1),
                            struct.pack("=qqHHi", 0, 22, 1, 0x110, 0),
                        )
                    ]
                events = self.Events(records)
                original_command = client.command

                def command(name, parameters=None):
                    if phase in ("setup", "final") or (
                        name == "WebDriver:ExecuteScript"
                        and parameters == gate.SNAPSHOT_PARAMETERS
                    ):
                        raise transport.GateError("transport-test-failure")
                    return original_command(name, parameters)

                output = io.StringIO()
                args = [
                    "--nonce",
                    "0123456789abcdef",
                    "--cycle",
                    "3",
                    "--firefox-pid",
                    "42",
                ]
                if phase == "final":
                    args.append("--verify-final")
                with (
                    mock.patch.object(gate, "validate_firefox_namespace"),
                    mock.patch.object(
                        gate,
                        "_connect",
                        return_value=client,
                        side_effect=transport.GateError("transport-test-failure")
                        if phase == "connect"
                        else None,
                    ),
                    mock.patch.object(gate, "RealEvdevSource", return_value=events),
                    mock.patch.object(
                        gate,
                        "DomStageServer",
                        return_value=self.Stages(
                            gate.dom_stage_title(
                                nonce="0123456789abcdef",
                                cycle=3,
                                stage="complete",
                            )
                        ),
                    ),
                    mock.patch.object(client, "command", side_effect=command),
                    mock.patch("sys.stdout", output),
                ):
                    try:
                        result = gate.main(args)
                    except transport.GateError as error:
                        self.fail(
                            f"transport failure escaped the guest contract: {error}"
                        )
                self.assertEqual(result, 1)
                self.assertEqual(output.getvalue().count("GRAPHICS_FAIL"), 1)
                self.assertIn("reason=transport-test-failure", output.getvalue())
                self.assertNotIn("GRAPHICS_PASS", output.getvalue())
                self.assertNotIn("__ASTERINAS_PHYSICAL_FINAL__", output.getvalue())
                self.assertEqual(client.close.call_count, int(phase != "connect"))
                self.assertEqual(events.closed, phase in ("setup", "snapshot"))

    class Client:
        def __init__(self, snapshot: dict[str, object]) -> None:
            self.snapshot = snapshot
            self.screenshot = png_payload(value=int(snapshot["cycle"]) + 0x30)
            self.viewport = (1920, 1080)
            self.calls: list[tuple[str, object | None]] = []
            self.timeouts: list[float] = []

        def set_timeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def command(self, name: str, parameters: object | None = None) -> object:
            self.calls.append((name, parameters))
            if name == "WebDriver:NewSession":
                return {"sessionId": "physical-session"}
            if name == "WebDriver:GetWindowHandles":
                return {"value": ["physical-window"]}
            if name == "WebDriver:GetWindowRect":
                return {"value": {"x": 0, "y": 0, "width": 1920, "height": 1080}}
            if name == "WebDriver:FullscreenWindow":
                return {"value": {"x": 0, "y": 0, "width": 1920, "height": 1080}}
            if name == "WebDriver:Navigate":
                return {"value": None}
            if name == "WebDriver:ExecuteScript":
                assert isinstance(parameters, dict)
                if "focus" in str(parameters.get("script")):
                    return {"value": "focused"}
                if "window.innerWidth" in str(parameters.get("script")):
                    width, height = self.viewport
                    return {"value": {"width": width, "height": height}}
                return {"value": self.snapshot}
            if name == "WebDriver:TakeScreenshot":
                return {"value": base64.b64encode(self.screenshot).decode()}
            raise AssertionError(f"unexpected Marionette command: {name}")

    class Events:
        def __init__(self, records: list[bytes]) -> None:
            self.records = records
            self.drained = False
            self.closed = False

        def drain(self) -> None:
            self.drained = True

        def poll(self, _timeout: float) -> list[bytes]:
            records, self.records = self.records, []
            return records

        def close(self) -> None:
            self.closed = True

    class Stages:
        def __init__(self, title: str) -> None:
            self.title = title
            self.page_url = "http://127.0.0.1:12345/index.html?cycle=3&nonce_length=16"

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info) -> None:
            return None

        def read_title(self, _timeout: float) -> str:
            return self.title

    def test_waits_for_target_document_before_ready_without_resetting_setup_budget(
        self,
    ) -> None:
        gate = load_gate(self)
        nonce = "0123456789abcdef"
        client = self.Client(PhysicalGraphicsSnapshotTests._snapshot(nonce, 1))
        events = self.Events([])
        markers: list[str] = []
        replies = iter((None, "loading", "focused"))
        original_command = client.command

        def command(name, parameters=None):
            if (
                name == "WebDriver:ExecuteScript"
                and parameters["filename"] == "asterinas-physical-graphics-prepare"
            ):
                client.calls.append((name, parameters))
                return {"value": next(replies)}
            return original_command(name, parameters)

        class ReadyReached(Exception):
            pass

        def emit(marker):
            markers.append(marker)
            if marker.startswith("ASTERINAS_PHYSICAL_GRAPHICS_READY "):
                raise ReadyReached

        with (
            mock.patch.object(client, "command", side_effect=command),
            mock.patch.object(gate.time, "sleep") as sleep,
            self.assertRaises(ReadyReached),
        ):
            try:
                gate.run_cycle(
                    client,
                    events,
                    stages=self.Stages(
                        gate.dom_stage_title(nonce=nonce, cycle=1, stage="complete")
                    ),
                    nonce=nonce,
                    cycle=1,
                    timeout=5,
                    emit=emit,
                )
            except gate.GateError as error:
                self.fail(f"transient document state rejected before READY: {error}")

        focus_calls = [
            parameters
            for name, parameters in client.calls
            if name == "WebDriver:ExecuteScript"
            and parameters["filename"] == "asterinas-physical-graphics-prepare"
        ]
        self.assertEqual(len(focus_calls), 3)
        for parameters in focus_calls:
            self.assertEqual(
                parameters["args"], [f"{gate.PAGE_URL}?cycle=1&nonce_length=16"]
            )
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(client.timeouts, [5])
        self.assertTrue(events.drained)
        self.assertTrue(events.closed)
        self.assertEqual(sum("GRAPHICS_READY" in marker for marker in markers), 1)

    def test_pending_document_expires_without_ready_or_input_drain(self) -> None:
        gate = load_gate(self)
        client = self.Client(
            PhysicalGraphicsSnapshotTests._snapshot("0123456789abcdef", 1)
        )
        events = self.Events([])
        markers: list[str] = []
        original_command = client.command

        def command(name, parameters=None):
            if name == "WebDriver:ExecuteScript":
                return {"value": "loading"}
            return original_command(name, parameters)

        with (
            mock.patch.object(client, "command", side_effect=command),
            mock.patch.object(
                gate.time,
                "monotonic",
                side_effect=[100, 100 + gate.MAX_SETUP_TIMEOUT + 1],
            ),
            mock.patch.object(gate.time, "sleep") as sleep,
            self.assertRaisesRegex(
                gate.GateError, "physical-graphics-document-timeout"
            ),
        ):
            gate.run_cycle(
                client,
                events,
                stages=self.Stages(
                    gate.dom_stage_title(
                        nonce="0123456789abcdef", cycle=1, stage="waiting"
                    )
                ),
                nonce="0123456789abcdef",
                cycle=1,
                timeout=5,
                emit=markers.append,
            )
        self.assertEqual(client.timeouts, [])
        self.assertFalse(events.drained)
        self.assertTrue(events.closed)
        self.assertFalse(any("GRAPHICS_READY" in marker for marker in markers))
        sleep.assert_not_called()

    def test_focus_script_checks_document_identity_and_completion_first(self) -> None:
        gate = load_gate(self)
        self.assertIn("document.URL", gate.FOCUS_SCRIPT)
        self.assertIn("arguments[0]", gate.FOCUS_SCRIPT)
        self.assertIn("document.readyState", gate.FOCUS_SCRIPT)
        self.assertLess(
            gate.FOCUS_SCRIPT.index("document.readyState"),
            gate.FOCUS_SCRIPT.index("input.focus()"),
        )

    def test_run_cycle_correlates_evdev_dom_and_png_after_ready(self) -> None:
        gate = load_gate(self)
        self.assertTrue(hasattr(gate, "run_cycle"), "guest cycle runner is missing")
        nonce = "0123456789abcdef"
        snapshot = PhysicalGraphicsSnapshotTests._snapshot(nonce, 2)
        client = self.Client(snapshot)
        records = [
            gate.INPUT_EVENT_STRUCT.pack(0, index, gate.EV_KEY, 30, 1)
            for index in range(len(nonce))
        ] + [
            gate.INPUT_EVENT_STRUCT.pack(0, 20, gate.EV_REL, gate.REL_Y, 3),
            gate.INPUT_EVENT_STRUCT.pack(0, 21, gate.EV_KEY, gate.BTN_LEFT, 1),
            gate.INPUT_EVENT_STRUCT.pack(0, 22, gate.EV_KEY, gate.BTN_LEFT, 0),
        ]
        events = self.Events(records)
        markers: list[str] = []
        result = gate.run_cycle(
            client,
            events,
            stages=self.Stages(
                gate.dom_stage_title(nonce=nonce, cycle=2, stage="complete")
            ),
            nonce=nonce,
            cycle=2,
            timeout=5.0,
            emit=markers.append,
        )
        self.assertTrue(events.drained)
        self.assertTrue(events.closed)
        self.assertEqual(result.key_downs, len(nonce))
        self.assertEqual(result.relative_events, 1)
        self.assertEqual(result.absolute_events, 0)
        self.assertEqual(client.timeouts, [5.0])
        self.assertEqual(len(markers), 22)
        self.assertEqual(
            markers[:12],
            [
                f"ASTERINAS_PHYSICAL_SETUP cycle=2 phase={phase} state={state}"
                for phase in (
                    "WebDriver:NewSession",
                    "WebDriver:GetWindowHandles",
                    "WebDriver:Navigate",
                    "WebDriver:ExecuteScript",
                    "WebDriver:GetWindowRect",
                    "WebDriver:ExecuteScript",
                )
                for state in ("start", "done")
            ],
        )
        self.assertTrue(
            markers[12].startswith("ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=2 ")
        )
        self.assertTrue(
            markers[13].startswith("ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle=2 ")
        )
        self.assertEqual(
            markers[14], "ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle=2"
        )
        self.assertEqual(markers[-1], "ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle=2")
        digest = hashlib.sha256(client.screenshot).hexdigest()
        self.assertEqual(
            markers[-4],
            f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=2 "
            f"size={len(client.screenshot)} sha256={digest}",
        )
        self.assertEqual(base64.b64decode(markers[-3]), client.screenshot)
        self.assertEqual(markers[-2], "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=2")
        names = [name for name, _ in client.calls]
        ready_index = names.index("WebDriver:GetWindowRect") + 2
        self.assertEqual(
            names[:ready_index],
            [
                "WebDriver:NewSession",
                "WebDriver:GetWindowHandles",
                "WebDriver:Navigate",
                "WebDriver:ExecuteScript",
                "WebDriver:GetWindowRect",
                "WebDriver:ExecuteScript",
            ],
        )
        self.assertEqual(
            client.calls[0][1],
            {
                "pageLoadStrategy": "none",
                "strictFileInteractability": True,
            },
        )
        self.assertEqual(
            names[ready_index:],
            [
                "WebDriver:ExecuteScript",
                "WebDriver:TakeScreenshot",
            ],
        )

    def test_main_reserves_setup_budget_without_reducing_interaction_budget(
        self,
    ) -> None:
        gate = load_gate(self)
        client = mock.Mock()
        events = mock.Mock()
        with (
            mock.patch.object(gate, "validate_firefox_namespace"),
            mock.patch.object(gate, "_connect", return_value=client) as connect,
            mock.patch.object(gate, "RealEvdevSource", return_value=events),
            mock.patch.object(
                gate,
                "DomStageServer",
                return_value=self.Stages(
                    gate.dom_stage_title(
                        nonce="0123456789abcdef", cycle=1, stage="complete"
                    )
                ),
            ),
            mock.patch.object(gate, "run_cycle") as run_cycle,
            mock.patch.object(gate.time, "monotonic", return_value=100.0),
        ):
            result = gate.main(
                [
                    "--nonce",
                    "0123456789abcdef",
                    "--cycle",
                    "1",
                    "--firefox-pid",
                    "42",
                    "--timeout",
                    "180",
                    "--setup-timeout",
                    "300",
                ]
            )

        self.assertEqual(result, 0)
        connect.assert_called_once_with("127.0.0.1", 2828, 400.0)
        self.assertEqual(run_cycle.call_args.kwargs["timeout"], 180.0)

    def test_run_cycle_accepts_qemu_tablet_and_requested_geometry(self) -> None:
        gate = load_gate(self)
        nonce = "0123456789abcdef"
        snapshot = PhysicalGraphicsSnapshotTests._snapshot(nonce, 1)
        client = self.Client(snapshot)
        client.screenshot = png_payload(width=1280, height=1024, value=0x42)
        records = [
            gate.INPUT_EVENT_STRUCT.pack(0, index, gate.EV_KEY, 30, 1)
            for index in range(len(nonce))
        ] + [
            gate.INPUT_EVENT_STRUCT.pack(0, 20, gate.EV_ABS, gate.ABS_X, 32767),
            gate.INPUT_EVENT_STRUCT.pack(0, 21, gate.EV_KEY, gate.BTN_LEFT, 1),
            gate.INPUT_EVENT_STRUCT.pack(0, 22, gate.EV_KEY, gate.BTN_LEFT, 0),
        ]
        events = self.Events(records)
        markers: list[str] = []

        with mock.patch.object(
            client,
            "command",
            wraps=client.command,
        ) as command:
            command.side_effect = lambda name, parameters=None: (
                {"value": {"x": 0, "y": 0, "width": 1280, "height": 1024}}
                if name == "WebDriver:FullscreenWindow"
                else mock.DEFAULT
            )
            result = gate.run_cycle(
                client,
                events,
                stages=self.Stages(
                    gate.dom_stage_title(nonce=nonce, cycle=1, stage="complete")
                ),
                nonce=nonce,
                cycle=1,
                timeout=5.0,
                expected_width=1280,
                expected_height=1024,
                emit=markers.append,
            )

        self.assertEqual(result.relative_events, 0)
        self.assertEqual(result.absolute_events, 1)
        self.assertIn(
            "WebDriver:FullscreenWindow",
            [call.args[0] for call in command.call_args_list],
        )
        self.assertIn("absolute_events=1", markers[-7])
        self.assertEqual(base64.b64decode(markers[-3]), client.screenshot)
        self.assertEqual(markers[-2], "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=1")

    def test_run_cycle_closes_input_source_on_snapshot_failure(self) -> None:
        gate = load_gate(self)
        self.assertTrue(hasattr(gate, "run_cycle"), "guest cycle runner is missing")
        client = self.Client(
            PhysicalGraphicsSnapshotTests._snapshot("fedcba9876543210", 1)
        )
        events = self.Events([])
        with self.assertRaisesRegex(gate.GateError, r"key_downs=0:.*stage=waiting"):
            gate.run_cycle(
                client,
                events,
                stages=self.Stages(
                    gate.dom_stage_title(
                        nonce="0123456789abcdef", cycle=1, stage="waiting"
                    )
                ),
                nonce="0123456789abcdef",
                cycle=1,
                timeout=0.01,
                emit=lambda _marker: None,
            )
        self.assertTrue(events.closed)

    def test_final_state_verifier_only_reads_existing_cycle_three_dom(self) -> None:
        gate = load_gate(self)
        nonce = "0011223344556677"
        client = self.Client(PhysicalGraphicsSnapshotTests._snapshot(nonce, 3))
        gate.verify_final_state(client, nonce=nonce, cycle=3)
        self.assertEqual(
            [name for name, _ in client.calls],
            ["WebDriver:NewSession", "WebDriver:ExecuteScript"],
        )


if __name__ == "__main__":
    unittest.main()
