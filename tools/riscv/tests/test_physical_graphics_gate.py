#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the deterministic physical graphics interaction witness."""

from __future__ import annotations

import importlib
import base64
from pathlib import Path
import sys
import tempfile
import unittest


ROOTFS_DIRECTORY = Path(__file__).parents[1] / "debian" / "rootfs"
PAGE = ROOTFS_DIRECTORY / "physical_graphics_interaction.html"
GATE = ROOTFS_DIRECTORY / "physical_graphics_gate.py"


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
    def test_counts_keyboard_motion_and_one_ordered_left_click(self) -> None:
        gate = load_gate(self)
        cycle = gate.EvdevCycle()
        cycle.feed(gate.InputEvent(0, 0, gate.EV_KEY, 30, 1))
        cycle.feed(gate.InputEvent(0, 0, gate.EV_REL, gate.REL_X, 4))
        cycle.feed(gate.InputEvent(0, 0, gate.EV_KEY, gate.BTN_LEFT, 1))
        cycle.feed(gate.InputEvent(0, 0, gate.EV_KEY, gate.BTN_LEFT, 0))
        self.assertEqual(cycle.key_downs, 1)
        self.assertEqual(cycle.relative_events, 1)
        self.assertTrue(cycle.left_click_complete)

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


class PhysicalGraphicsRunTests(unittest.TestCase):
    class Client:
        def __init__(self, snapshot: dict[str, object]) -> None:
            self.snapshot = snapshot
            self.calls: list[tuple[str, object | None]] = []

        def command(self, name: str, parameters: object | None = None) -> object:
            self.calls.append((name, parameters))
            if name == "WebDriver:NewSession":
                return {"sessionId": "physical-session"}
            if name == "WebDriver:Navigate":
                return {"value": None}
            if name == "WebDriver:ExecuteScript":
                assert isinstance(parameters, dict)
                if "focus" in str(parameters.get("script")):
                    return {"value": "focused"}
                return {"value": self.snapshot}
            if name == "WebDriver:TakeScreenshot":
                return {"value": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()}
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
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cycle-2.png"
            result = gate.run_cycle(
                client,
                events,
                nonce=nonce,
                cycle=2,
                timeout=5.0,
                screenshot=output,
                emit=markers.append,
            )
            self.assertEqual(output.read_bytes(), b"\x89PNG\r\n\x1a\nimage")
        self.assertTrue(events.drained)
        self.assertTrue(events.closed)
        self.assertEqual(result.key_downs, len(nonce))
        self.assertEqual(result.relative_events, 1)
        self.assertEqual(len(markers), 5)
        self.assertTrue(
            markers[0].startswith("ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=2 ")
        )
        self.assertEqual(markers[-1], "ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle=2")
        names = [name for name, _ in client.calls]
        ready_index = names.index("WebDriver:ExecuteScript") + 1
        self.assertEqual(
            names[:ready_index],
            [
                "WebDriver:NewSession",
                "WebDriver:Navigate",
                "WebDriver:ExecuteScript",
            ],
        )
        self.assertEqual(
            names[ready_index:],
            [
                "WebDriver:ExecuteScript",
                "WebDriver:TakeScreenshot",
            ],
        )

    def test_run_cycle_closes_input_source_on_snapshot_failure(self) -> None:
        gate = load_gate(self)
        self.assertTrue(hasattr(gate, "run_cycle"), "guest cycle runner is missing")
        client = self.Client(
            PhysicalGraphicsSnapshotTests._snapshot("fedcba9876543210", 1)
        )
        events = self.Events([])
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(gate.GateError),
        ):
            gate.run_cycle(
                client,
                events,
                nonce="0123456789abcdef",
                cycle=1,
                timeout=0.01,
                screenshot=Path(directory) / "cycle-1.png",
                emit=lambda _marker: None,
            )
        self.assertTrue(events.closed)


if __name__ == "__main__":
    unittest.main()
