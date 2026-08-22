# SPDX-License-Identifier: MPL-2.0

import dataclasses
import io
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import boot_keyboard_oracle as oracle  # noqa: E402


class ScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.by_name = {scenario.name: scenario for scenario in oracle.SCENARIOS}

    def test_report_builds_an_eight_byte_tuple(self):
        value = oracle.report(0x04, 0x05, modifier=0x02, reserved=0x7F)

        self.assertEqual(value, (0x02, 0x7F, 0x04, 0x05, 0, 0, 0, 0))
        self.assertIs(type(value), tuple)
        self.assertEqual(oracle.EMPTY_REPORT, (0,) * 8)

    def test_report_rejects_invalid_fields(self):
        invalid_calls = (
            lambda: oracle.report(-1),
            lambda: oracle.report(0x100),
            lambda: oracle.report(True),
            lambda: oracle.report(modifier=-1),
            lambda: oracle.report(modifier=0x100),
            lambda: oracle.report(modifier=True),
            lambda: oracle.report(reserved=-1),
            lambda: oracle.report(reserved=0x100),
            lambda: oracle.report(reserved=True),
            lambda: oracle.report(4, 5, 6, 7, 8, 9, 10),
        )

        for invalid_call in invalid_calls:
            with self.subTest(call=invalid_call):
                with self.assertRaises((TypeError, ValueError)):
                    invalid_call()

    def test_scenarios_are_immutable(self):
        scenario = oracle.Scenario("immutable", (oracle.EMPTY_REPORT,))

        with self.assertRaises(dataclasses.FrozenInstanceError):
            scenario.name = "changed"
        self.assertIs(type(scenario.reports), tuple)

    def test_usage_sweep_is_exact(self):
        expected_names = [f"usage_{usage:02x}" for usage in range(0x04, 0x66)]
        actual = [
            scenario
            for scenario in oracle.SCENARIOS
            if scenario.name.startswith("usage_")
        ]

        self.assertEqual([scenario.name for scenario in actual], expected_names)
        for usage, scenario in zip(range(0x04, 0x66), actual, strict=True):
            pressed = oracle.report(usage)
            self.assertEqual(
                scenario.reports,
                (pressed, pressed, oracle.EMPTY_REPORT),
                scenario.name,
            )

    def test_modifier_scenarios_are_exact(self):
        for bit in range(8):
            pressed = oracle.report(modifier=1 << bit)
            self.assertEqual(
                self.by_name[f"modifier_{bit}"].reports,
                (pressed, pressed, oracle.EMPTY_REPORT),
            )

        all_pressed = oracle.report(0x04, modifier=0xFF)
        self.assertEqual(
            self.by_name["all_modifiers"].reports,
            (all_pressed, all_pressed, oracle.EMPTY_REPORT),
        )

    def test_chord_scenarios_are_exact(self):
        for key_count in range(2, 7):
            modifier = 0x01 if key_count == 4 else 0
            pressed = oracle.report(
                *range(0x04, 0x04 + key_count),
                modifier=modifier,
            )
            self.assertEqual(
                self.by_name[f"chord_{key_count}"].reports,
                (pressed, pressed, oracle.EMPTY_REPORT),
            )

    def test_named_edge_cases_have_exact_reports(self):
        empty = oracle.EMPTY_REPORT
        a = oracle.report(0x04)
        b = oracle.report(0x05)
        ab = oracle.report(0x04, 0x05)
        ac = oracle.report(0x04, 0x06)
        abc = oracle.report(0x04, 0x05, 0x06)
        backslash_31 = oracle.report(0x31)
        backslash_32 = oracle.report(0x32)
        both_backslashes = oracle.report(0x31, 0x32)

        expected = {
            "zero_usage": (oracle.report(0), empty),
            "add_remove_modifier": (
                a,
                oracle.report(0x04, modifier=0x02),
                a,
                empty,
            ),
            "simultaneous_modifier_release": (
                oracle.report(0x04, modifier=0xFF),
                empty,
            ),
            "shift_a": (oracle.report(0x04, modifier=0x02), empty),
            "ctrl_alt_delete": (oracle.report(0x4C, modifier=0x05), empty),
            "six_key_partial_release": (
                oracle.report(0x04, 0x05, 0x06, 0x07, 0x08, 0x09),
                oracle.report(0x04, 0x06, 0x08),
                empty,
            ),
            "add_to_chord": (ab, abc, empty),
            "release_one_from_chord": (abc, ac, empty),
            "replace_subset": (
                abc,
                oracle.report(0x04, 0x07, 0x08, modifier=0x01),
                oracle.report(modifier=0x01),
                empty,
            ),
            "reordered_array": (ab, oracle.report(0x05, 0x04), empty),
            "duplicate_usage": (oracle.report(0x04, 0x04), empty),
            "zero_filled_holes": (oracle.report(0x04, 0, 0x05, 0), empty),
            "replace_a_with_b": (a, b, empty),
            "backslash_alias_keycode_state": (
                both_backslashes,
                backslash_31,
                empty,
                backslash_31,
                both_backslashes,
                backslash_32,
                empty,
                both_backslashes,
                oracle.report(0x32, 0x31),
                empty,
            ),
            "reserved_byte_change": (
                oracle.report(modifier=0x01),
                oracle.report(modifier=0x01, reserved=0x7F),
                empty,
            ),
            "omitted_intermediate_report": (a, oracle.report(0x06), empty),
            "unsupported_66": (oracle.report(0x66), empty),
            "unsupported_ff": (oracle.report(0xFF), empty),
        }

        for name, reports in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self.by_name[name].reports, reports)

    def test_error_scenarios_are_exact(self):
        a = oracle.report(0x04)
        b = oracle.report(0x05)
        empty = oracle.EMPTY_REPORT

        self.assertEqual(oracle.ERROR_USAGES, (1, 2, 3))
        for error_usage in oracle.ERROR_USAGES:
            errors = oracle.report(*([error_usage] * 6))
            shifted_errors = oracle.report(*([error_usage] * 6), modifier=0x02)
            self.assertEqual(
                self.by_name[f"error_{error_usage}_empty"].reports,
                (errors, empty),
            )
            self.assertEqual(
                self.by_name[f"error_{error_usage}_held"].reports,
                (a, errors, b, empty),
            )
            self.assertEqual(
                self.by_name[f"error_{error_usage}_modifier"].reports,
                (a, shifted_errors, a, empty),
            )

    def test_scenario_names_and_report_shapes_are_valid(self):
        names = [scenario.name for scenario in oracle.SCENARIOS]

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 139)
        for scenario in oracle.SCENARIOS:
            with self.subTest(name=scenario.name):
                self.assertTrue(scenario.reports)
                self.assertEqual(scenario.reports[-1], oracle.EMPTY_REPORT)
                for report_bytes in scenario.reports:
                    self.assertIs(type(report_bytes), tuple)
                    self.assertEqual(len(report_bytes), 8)
                    self.assertTrue(
                        all(
                            type(byte) is int and 0 <= byte <= 0xFF
                            for byte in report_bytes
                        )
                    )


class NormalizationTests(unittest.TestCase):
    def test_normalize_drops_scan_metadata_and_keeps_key_order(self):
        events = (
            oracle.RawEvent(oracle.EV_MSC, oracle.MSC_SCAN, 0x70004),
            oracle.RawEvent(oracle.EV_KEY, 30, 1),
            oracle.RawEvent(oracle.EV_MSC, oracle.MSC_SCAN, 0x70005),
            oracle.RawEvent(oracle.EV_KEY, 48, 1),
            oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),
        )

        self.assertEqual(
            oracle.normalize_events(events),
            (
                oracle.RawEvent(oracle.EV_KEY, 30, 1),
                oracle.RawEvent(oracle.EV_KEY, 48, 1),
                oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),
            ),
        )

    def test_normalize_drops_intermediate_sync_value_one(self):
        events = (
            oracle.RawEvent(oracle.EV_KEY, 30, 1),
            oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 1),
            oracle.RawEvent(oracle.EV_KEY, 48, 1),
            oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),
        )

        self.assertEqual(
            oracle.normalize_events(events),
            (
                oracle.RawEvent(oracle.EV_KEY, 30, 1),
                oracle.RawEvent(oracle.EV_KEY, 48, 1),
                oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),
            ),
        )

    def test_normalize_keeps_terminal_sync_after_key_change(self):
        terminal = oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0)
        key = oracle.RawEvent(oracle.EV_KEY, 30, 0)

        self.assertEqual(oracle.normalize_events((key, terminal)), (key, terminal))

    def test_normalize_drops_an_empty_terminal_frame(self):
        terminal = oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0)

        self.assertEqual(oracle.normalize_events((terminal,)), ())
        self.assertEqual(
            oracle.normalize_events(
                (oracle.RawEvent(oracle.EV_MSC, oracle.MSC_SCAN, 0), terminal)
            ),
            (),
        )

    def test_normalize_rejects_every_other_event(self):
        unexpected_events = (
            oracle.RawEvent(2, 0, 1),
            oracle.RawEvent(oracle.EV_MSC, 5, 1),
            oracle.RawEvent(oracle.EV_SYN, 1, 0),
            oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 2),
        )

        for event in unexpected_events:
            with self.subTest(event=event):
                with self.assertRaises(oracle.OracleError):
                    oracle.normalize_events((event,))

    def test_normalize_rejects_key_autorepeat(self):
        events = (
            oracle.RawEvent(oracle.EV_KEY, 30, 2),
            oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),
        )

        with self.assertRaisesRegex(oracle.OracleError, "value=2"):
            oracle.normalize_events(events)


class RustRenderingTests(unittest.TestCase):
    def setUp(self):
        self.provenance = oracle.Provenance(
            generation_command=oracle.GENERATION_COMMAND,
            kernel_release="6.16.0-test",
            hid_tools_version="0.12",
            evdev_version="1.9.3",
            usb_hid_version="1.11",
            hid_usage_tables_version="1.7",
            descriptor_sha256="d" * 64,
            scenarios_sha256="s" * 64,
        )
        self.capture = oracle.ScenarioCapture(
            'quote"\n\N{SNOWMAN}',
            (
                oracle.StepCapture(
                    oracle.report(0x04, modifier=0x02),
                    (
                        oracle.RawEvent(oracle.EV_KEY, 30, 1),
                        oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),
                    ),
                ),
                oracle.StepCapture(oracle.EMPTY_REPORT, ()),
            ),
        )

    def assert_rustfmt_clean(self, content):
        with tempfile.TemporaryDirectory() as temp_dir:
            rust_path = Path(temp_dir) / "vectors.rs"
            rust_path.write_text(content, encoding="utf-8")
            completed = subprocess.run(
                ["rustfmt", "--check", rust_path],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    def test_capture_and_provenance_records_are_immutable(self):
        records = (
            oracle.RawEvent(1, 30, 1),
            oracle.StepCapture(oracle.EMPTY_REPORT, ()),
            oracle.ScenarioCapture("scenario", ()),
            self.provenance,
        )

        for record in records:
            with self.subTest(record=record):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(record, next(iter(record.__dict__)), "changed")

    def test_render_rust_is_deterministic_and_contains_raw_triplets(self):
        captures = (self.capture, dataclasses.replace(self.capture, name="second"))
        first = oracle.render_rust(captures, self.provenance)
        second = oracle.render_rust(captures, self.provenance)

        self.assertEqual(first, second)
        self.assertIn("// SPDX-License-Identifier: MPL-2.0\n", first)
        self.assertIn("// @generated; do not edit.\n", first)
        self.assertIn(f"// Generation command: {oracle.GENERATION_COMMAND}\n", first)
        self.assertIn("// Linux kernel: 6.16.0-test\n", first)
        self.assertIn("// hid-tools: 0.12\n", first)
        self.assertIn("// evdev: 1.9.3\n", first)
        self.assertIn("// USB HID: 1.11\n", first)
        self.assertIn("// HID Usage Tables: 1.7\n", first)
        self.assertIn(f"// Descriptor SHA256: {'d' * 64}\n", first)
        self.assertIn(f"// Scenarios SHA256: {'s' * 64}\n", first)
        self.assertIn("pub(super) struct LinuxStep {", first)
        self.assertIn("report: [u8; 8]", first)
        self.assertIn("events: &'static [(u16, u16, i32)]", first)
        self.assertIn("pub(super) struct LinuxScenario {", first)
        self.assertIn("pub(super) static LINUX_SCENARIOS", first)
        self.assertIn('name: "quote\\"\\n☃"', first)
        self.assertIn(
            "report: [0x02, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00]",
            first,
        )
        self.assertIn("events: &[(1, 30, 1), (0, 0, 0)]", first)
        self.assertNotIn("\t", first)
        self.assertTrue(first.endswith("\n"))
        self.assertFalse(first.endswith("\n\n"))
        self.assert_rustfmt_clean(first)

    def test_render_rust_wraps_long_event_slices(self):
        events = tuple(
            oracle.RawEvent(oracle.EV_KEY, code, 1)
            for code in (29, 30, 42, 48, 54, 56, 97, 100, 125, 126)
        ) + (oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),)
        capture = oracle.ScenarioCapture(
            "many_events",
            (
                oracle.StepCapture(oracle.report(0x04), events),
                oracle.StepCapture(oracle.EMPTY_REPORT, ()),
            ),
        )

        captures = (capture, dataclasses.replace(capture, name="second"))
        rendered = oracle.render_rust(captures, self.provenance)

        self.assertIn(
            "                events: &[\n"
            "                    (1, 29, 1),\n"
            "                    (1, 30, 1),\n",
            rendered,
        )
        self.assertIn("                    (0, 0, 0),\n                ],", rendered)
        self.assertTrue(all(len(line) <= 100 for line in rendered.splitlines()))
        self.assert_rustfmt_clean(rendered)

    def test_render_rust_respects_rustfmt_array_width(self):
        events = tuple(
            oracle.RawEvent(oracle.EV_KEY, code, 1) for code in (29, 30, 42, 48, 54)
        ) + (oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),)
        capture = oracle.ScenarioCapture(
            "array_width",
            (
                oracle.StepCapture(oracle.report(0x04), events),
                oracle.StepCapture(oracle.EMPTY_REPORT, ()),
            ),
        )
        captures = (capture, dataclasses.replace(capture, name="second"))

        rendered = oracle.render_rust(captures, self.provenance)

        self.assertIn("                events: &[\n", rendered)
        self.assert_rustfmt_clean(rendered)


class PublicationAndProvenanceTests(unittest.TestCase):
    def test_requirements_are_exactly_pinned(self):
        requirements = (TOOL_DIR / "requirements.txt").read_text(encoding="utf-8")

        self.assertEqual(requirements, "evdev==1.9.3\nhid-tools==0.12\n")

    def test_descriptor_is_strict_hid_1_11_boot_keyboard(self):
        expected = bytes.fromhex(
            "05 01 09 06 A1 01 05 07 19 E0 29 E7 15 00 25 01 "
            "75 01 95 08 81 02 95 01 75 08 81 01 95 05 75 01 "
            "05 08 19 01 29 05 91 02 95 01 75 03 91 01 95 06 "
            "75 08 15 00 25 65 05 07 19 00 29 65 81 00 C0"
        )

        self.assertEqual(oracle.BOOT_KEYBOARD_DESCRIPTOR, expected)
        self.assertEqual(oracle.BOOT_KEYBOARD_DESCRIPTOR.count(bytes((0x65,))), 2)

    def test_scenario_hash_is_stable_and_content_sensitive(self):
        scenarios = (
            oracle.Scenario("one", (oracle.report(4), oracle.EMPTY_REPORT)),
            oracle.Scenario("two", (oracle.report(5), oracle.EMPTY_REPORT)),
        )

        first = oracle.scenario_sha256(scenarios)
        second = oracle.scenario_sha256(tuple(scenarios))
        changed_name = oracle.scenario_sha256(
            (oracle.Scenario("changed", scenarios[0].reports), scenarios[1])
        )
        changed_report = oracle.scenario_sha256(
            (
                scenarios[0],
                oracle.Scenario("two", (oracle.report(6), oracle.EMPTY_REPORT)),
            )
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed_name)
        self.assertNotEqual(first, changed_report)

    def test_publish_updates_then_checks_identical_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "vectors.rs"

            oracle.publish(output, "generated\n", check=False)
            oracle.publish(output, "generated\n", check=True)

            self.assertEqual(output.read_bytes(), b"generated\n")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_differing_check_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "vectors.rs"
            output.write_text("old\n", encoding="utf-8")

            with self.assertRaises(oracle.OracleError):
                oracle.publish(output, "new\n", check=True)

            self.assertEqual(output.read_text(encoding="utf-8"), "old\n")

    def test_check_detects_noncanonical_newlines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "vectors.rs"
            output.write_bytes(b"generated\r\n")

            with self.assertRaises(oracle.OracleError):
                oracle.publish(output, "generated\n", check=True)

            self.assertEqual(output.read_bytes(), b"generated\r\n")

    def test_check_rejects_a_missing_file_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "vectors.rs"

            with self.assertRaises(oracle.OracleError):
                oracle.publish(output, "new\n", check=True)

            self.assertFalse(output.exists())

    def test_publish_cleans_temporary_file_after_replace_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "vectors.rs"
            with mock.patch.object(oracle.os, "replace", side_effect=OSError("failed")):
                with self.assertRaises(oracle.OracleError):
                    oracle.publish(output, "new\n", check=False)

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_provenance_uses_versions_release_and_input_hashes(self):
        with (
            mock.patch.object(oracle.platform, "release", return_value="kernel-test"),
            mock.patch.object(
                oracle.importlib.metadata,
                "version",
                side_effect=lambda package: {
                    "hid-tools": "0.12",
                    "evdev": "1.9.3",
                }[package],
            ),
        ):
            provenance = oracle.make_provenance()

        self.assertEqual(provenance.generation_command, oracle.GENERATION_COMMAND)
        self.assertEqual(provenance.kernel_release, "kernel-test")
        self.assertEqual(provenance.hid_tools_version, "0.12")
        self.assertEqual(provenance.evdev_version, "1.9.3")
        self.assertEqual(provenance.usb_hid_version, "1.11")
        self.assertEqual(provenance.hid_usage_tables_version, "1.7")
        self.assertEqual(len(provenance.descriptor_sha256), 64)
        self.assertEqual(
            provenance.scenarios_sha256, oracle.scenario_sha256(oracle.SCENARIOS)
        )

    def test_import_does_not_load_linux_hid_dependencies(self):
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(TOOL_DIR)!r}); "
            "import boot_keyboard_oracle; "
            "assert 'evdev' not in sys.modules; "
            "assert 'hidtools' not in sys.modules; "
            "assert 'hidtools.uhid' not in sys.modules"
        )

        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


class FakeUhidDevice:
    instances = []
    node_paths = ["/dev/input/event-test"]
    lifecycle = []
    input_hook = None

    def __init__(self):
        self.uniq = "uhid_test_unique"
        self.device_nodes = list(self.node_paths)
        self.input_reports = []
        self.destroyed = False
        self.__class__.instances.append(self)

    @classmethod
    def dispatch(cls, timeout=None):
        cls.lifecycle.append(("dispatch", timeout))
        return False

    def create_kernel_device(self):
        self.__class__.lifecycle.append("create")

    def call_input_event(self, report_bytes):
        self.__class__.lifecycle.append("input")
        self.input_reports.append(tuple(report_bytes))
        if self.input_hook is not None:
            self.__class__.input_hook(report_bytes)

    def destroy(self):
        self.__class__.lifecycle.append("destroy")
        self.destroyed = True


class FakeInputDevice:
    instances = []
    read_batches = []
    lifecycle = FakeUhidDevice.lifecycle
    repeat_readback = None
    repeat_event_batch = None

    def __init__(self, node):
        self.node = node
        self.batches = [list(batch) for batch in self.read_batches]
        self._repeat = (250, 33)
        self.ungrabbed = False
        self.closed = False
        self.__class__.instances.append(self)

    def grab(self):
        self.lifecycle.append("grab")

    @property
    def repeat(self):
        value = self.repeat_readback or self._repeat
        self.lifecycle.append(("repeat_get", value))
        return value

    @repeat.setter
    def repeat(self, value):
        self._repeat = tuple(value)
        self.lifecycle.append(("repeat_set", self._repeat))
        repeat_event_batch = self.repeat_event_batch or (
            fake_event(20, 0, self._repeat[0]),
            fake_event(20, 1, self._repeat[1]),
            fake_event(0, 0, 0),
        )
        self.batches.insert(0, list(repeat_event_batch))

    def ungrab(self):
        self.lifecycle.append("ungrab")
        self.ungrabbed = True

    def close(self):
        self.lifecycle.append("evdev_close")
        self.closed = True

    def read(self):
        return iter(self.batches.pop(0))

    def fileno(self):
        return 17


def fake_event(event_type, code, value):
    return types.SimpleNamespace(type=event_type, code=code, value=value)


class LinuxUhidOracleTests(unittest.TestCase):
    def setUp(self):
        FakeUhidDevice.instances = []
        FakeUhidDevice.node_paths = ["/dev/input/event-test"]
        FakeUhidDevice.lifecycle = []
        FakeUhidDevice.input_hook = None
        FakeInputDevice.instances = []
        FakeInputDevice.read_batches = []
        FakeInputDevice.lifecycle = FakeUhidDevice.lifecycle
        FakeInputDevice.repeat_readback = None
        FakeInputDevice.repeat_event_batch = None
        self.uhid_module = types.SimpleNamespace(
            UHIDDevice=FakeUhidDevice,
            BusType=types.SimpleNamespace(USB=3),
        )
        self.evdev_module = types.SimpleNamespace(InputDevice=FakeInputDevice)
        self.module_patch = mock.patch.object(
            oracle,
            "_load_linux_modules",
            return_value=(self.uhid_module, self.evdev_module),
        )
        self.access_patch = mock.patch.object(oracle.os, "access", return_value=True)
        self.module_patch.start()
        self.access_patch.start()

    def tearDown(self):
        self.access_patch.stop()
        self.module_patch.stop()

    def test_context_configures_grabs_and_cleans_up_device(self):
        with (
            mock.patch.object(
                oracle.select,
                "select",
                return_value=([17], [], []),
            ),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            uhid_device = FakeUhidDevice.instances[0]
            input_device = FakeInputDevice.instances[0]
            self.assertEqual(
                uhid_device.name, "asterinas-boot-keyboard-uhid_test_unique"
            )
            self.assertEqual(uhid_device.phys, "asterinas/usb-hid-oracle")
            self.assertEqual(uhid_device.info, (3, 0x1D6B, 0xA57E))
            self.assertEqual(uhid_device.rdesc, oracle.BOOT_KEYBOARD_DESCRIPTOR)
            self.assertEqual(input_device.node, "/dev/input/event-test")
            self.assertIs(linux_oracle.uhid_device, uhid_device)
            self.assertLess(
                FakeUhidDevice.lifecycle.index("grab"),
                FakeUhidDevice.lifecycle.index("input")
                if "input" in FakeUhidDevice.lifecycle
                else len(FakeUhidDevice.lifecycle),
            )

        self.assertTrue(input_device.ungrabbed)
        self.assertTrue(input_device.closed)
        self.assertTrue(uhid_device.destroyed)
        self.assertLess(
            FakeUhidDevice.lifecycle.index("evdev_close"),
            FakeUhidDevice.lifecycle.index("destroy"),
        )

    def test_context_configures_and_verifies_safe_repeat_before_input(self):
        with (
            mock.patch.object(
                oracle.select,
                "select",
                side_effect=lambda readers, _writers, _errors, _timeout: (
                    (readers, [], [])
                    if FakeInputDevice.instances[0].batches
                    else ([], [], [])
                ),
            ),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            linux_oracle.capture_report(oracle.EMPTY_REPORT)

        lifecycle = FakeUhidDevice.lifecycle
        repeat = (60_000, 60_000)
        self.assertIn(("repeat_set", repeat), lifecycle)
        self.assertIn(("repeat_get", repeat), lifecycle)
        self.assertLess(
            lifecycle.index("grab"), lifecycle.index(("repeat_set", repeat))
        )
        self.assertLess(
            lifecycle.index(("repeat_set", repeat)),
            lifecycle.index(("repeat_get", repeat)),
        )
        self.assertLess(
            lifecycle.index(("repeat_get", repeat)), lifecycle.index("input")
        )

    def test_context_rejects_unaccepted_safe_repeat(self):
        FakeInputDevice.repeat_readback = (60_000, 59_999)

        with self.assertRaisesRegex(oracle.OracleError, "repeat"):
            with oracle.LinuxUhidOracle():
                self.fail("context body must not run")

        input_device = FakeInputDevice.instances[0]
        self.assertTrue(input_device.ungrabbed)
        self.assertTrue(input_device.closed)
        self.assertTrue(FakeUhidDevice.instances[0].destroyed)

    def test_context_drains_exact_repeat_configuration_frame(self):
        with (
            mock.patch.object(
                oracle.select,
                "select",
                side_effect=lambda readers, _writers, _errors, _timeout: (
                    (readers, [], [])
                    if FakeInputDevice.instances[0].batches
                    else ([], [], [])
                ),
            ),
            oracle.LinuxUhidOracle(),
        ):
            self.assertEqual(FakeInputDevice.instances[0].batches, [])

    def test_context_rejects_repeat_configuration_timeout(self):
        with (
            mock.patch.object(oracle.select, "select", return_value=([], [], [])),
            self.assertRaisesRegex(oracle.OracleError, "repeat.*timeout"),
        ):
            with oracle.LinuxUhidOracle():
                self.fail("context body must not run")

    def test_context_rejects_invalid_repeat_configuration_frame(self):
        expected = (
            fake_event(20, 0, 60_000),
            fake_event(20, 1, 60_000),
            fake_event(0, 0, 0),
        )
        invalid_batches = {
            "order": (expected[1], expected[0], expected[2]),
            "value": (
                expected[0],
                fake_event(20, 1, 59_999),
                expected[2],
            ),
            "extra": (*expected, fake_event(1, 30, 1)),
        }

        for case, batch in invalid_batches.items():
            with self.subTest(case=case):
                FakeInputDevice.repeat_event_batch = batch
                with (
                    mock.patch.object(
                        oracle.select,
                        "select",
                        return_value=([17], [], []),
                    ),
                    self.assertRaisesRegex(
                        oracle.OracleError,
                        "repeat configuration frame",
                    ),
                ):
                    with oracle.LinuxUhidOracle():
                        self.fail("context body must not run")

    def test_context_rejects_missing_uhid_permissions(self):
        with mock.patch.object(oracle.os, "access", return_value=False):
            with self.assertRaises(oracle.OracleError):
                with oracle.LinuxUhidOracle():
                    self.fail("context body must not run")

        self.assertEqual(FakeUhidDevice.instances, [])

    def test_context_rejects_multiple_evdev_nodes_and_destroys_uhid(self):
        FakeUhidDevice.node_paths = ["/dev/input/event-a", "/dev/input/event-b"]

        with self.assertRaises(oracle.OracleError):
            with oracle.LinuxUhidOracle():
                self.fail("context body must not run")

        self.assertTrue(FakeUhidDevice.instances[0].destroyed)
        self.assertEqual(FakeInputDevice.instances, [])

    def test_context_rejects_zero_evdev_nodes_and_destroys_uhid(self):
        FakeUhidDevice.node_paths = []

        with (
            mock.patch.object(oracle, "DEVICE_DISCOVERY_TIMEOUT_SECONDS", 0),
            self.assertRaises(oracle.OracleError),
        ):
            with oracle.LinuxUhidOracle():
                self.fail("context body must not run")

        self.assertTrue(FakeUhidDevice.instances[0].destroyed)

    def test_partial_setup_failure_closes_created_uhid_device(self):
        self.evdev_module.InputDevice = mock.Mock(side_effect=OSError("open failed"))

        with self.assertRaises(oracle.OracleError):
            with oracle.LinuxUhidOracle():
                self.fail("context body must not run")

        self.assertTrue(FakeUhidDevice.instances[0].destroyed)

    def test_capture_normalizes_events_through_terminal_sync(self):
        FakeInputDevice.read_batches = [
            (
                fake_event(oracle.EV_MSC, oracle.MSC_SCAN, 0x70004),
                fake_event(oracle.EV_KEY, 30, 1),
                fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 1),
                fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 0),
            )
        ]

        with (
            mock.patch.object(
                oracle.select,
                "select",
                side_effect=lambda readers, _writers, _errors, _timeout: (
                    (readers, [], [])
                    if FakeInputDevice.instances[0].batches
                    else ([], [], [])
                ),
            ),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            events = linux_oracle.capture_report(oracle.report(0x04))

        self.assertEqual(
            events,
            (
                oracle.RawEvent(oracle.EV_KEY, 30, 1),
                oracle.RawEvent(oracle.EV_SYN, oracle.SYN_REPORT, 0),
            ),
        )
        self.assertEqual(
            FakeUhidDevice.instances[0].input_reports, [oracle.report(0x04)]
        )

    def test_capture_rejects_events_after_terminal_sync_in_one_read(self):
        FakeInputDevice.read_batches = [
            (
                fake_event(oracle.EV_KEY, 30, 1),
                fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 0),
                fake_event(oracle.EV_KEY, 30, 0),
                fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 0),
            ),
        ]

        with (
            mock.patch.object(
                oracle.select,
                "select",
                return_value=([17], [], []),
            ),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            with self.assertRaisesRegex(oracle.OracleError, "terminal"):
                linux_oracle.capture_report(oracle.report(0x04))

    def test_capture_returns_empty_after_quiet_timeout(self):
        select_mock = mock.Mock(
            side_effect=[([17], [], []), ([], [], [])],
        )
        with (
            mock.patch.object(oracle.select, "select", select_mock),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            select_mock.reset_mock()
            events = linux_oracle.capture_report(oracle.EMPTY_REPORT)

        self.assertEqual(events, ())
        self.assertEqual(select_mock.call_count, 1)
        self.assertGreater(select_mock.call_args.args[3], 0)

    def test_capture_rejects_timeout_after_partial_frame(self):
        FakeInputDevice.read_batches = [
            (fake_event(oracle.EV_KEY, 30, 1),),
        ]

        with (
            mock.patch.object(
                oracle.select,
                "select",
                side_effect=[([17], [], []), ([17], [], []), ([], [], [])],
            ),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            with self.assertRaises(oracle.OracleError):
                linux_oracle.capture_report(oracle.report(0x04))

    def test_partial_batches_do_not_extend_report_deadline(self):
        FakeInputDevice.read_batches = [
            (fake_event(oracle.EV_KEY, 30, 1),),
        ]
        select_mock = mock.Mock(
            side_effect=[([17], [], []), ([17], [], []), ([], [], [])],
        )

        with (
            mock.patch.object(oracle.select, "select", select_mock),
            mock.patch.object(
                oracle.time,
                "monotonic",
                side_effect=(0.0, 0.10, 0.15, 0.24, 0.25),
            ),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            with self.assertRaises(oracle.OracleError):
                linux_oracle.capture_report(oracle.report(0x04))

        first_timeout = select_mock.call_args_list[1].args[3]
        second_timeout = select_mock.call_args_list[2].args[3]
        self.assertAlmostEqual(first_timeout, 0.15)
        self.assertAlmostEqual(second_timeout, 0.06)

    def test_capture_does_not_read_events_after_report_deadline(self):
        FakeInputDevice.read_batches = [
            (
                fake_event(oracle.EV_KEY, 30, 1),
                fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 1),
            ),
            (fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 0),),
        ]
        select_mock = mock.Mock(
            side_effect=[([17], [], []), ([17], [], [])],
        )

        with (
            mock.patch.object(oracle.select, "select", select_mock),
            mock.patch.object(
                oracle.time,
                "monotonic",
                side_effect=(0.0, 1.0, 1.05, 1.21),
            ),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            select_mock.reset_mock()
            with self.assertRaisesRegex(oracle.OracleError, "partial"):
                linux_oracle.capture_report(oracle.report(0x04))

        self.assertEqual(select_mock.call_count, 1)
        self.assertEqual(len(FakeInputDevice.instances[0].batches), 1)

    def test_injection_delay_rejects_queued_frame_and_cleans_up(self):
        FakeInputDevice.read_batches = [
            (
                fake_event(oracle.EV_KEY, 30, 2),
                fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 0),
            ),
        ]
        clock = [0.0]
        FakeUhidDevice.input_hook = lambda _report: clock.__setitem__(0, 0.21)
        select_mock = mock.Mock(return_value=([17], [], []))

        with (
            mock.patch.object(oracle.select, "select", select_mock),
            mock.patch.object(oracle.time, "monotonic", side_effect=lambda: clock[0]),
            oracle.LinuxUhidOracle() as linux_oracle,
        ):
            select_mock.reset_mock()
            input_device = FakeInputDevice.instances[0]
            uhid_device = FakeUhidDevice.instances[0]
            with self.assertRaisesRegex(oracle.OracleError, "deadline"):
                linux_oracle.capture_report(oracle.report(0x04))

        self.assertEqual(select_mock.call_count, 0)
        self.assertEqual(len(input_device.batches), 1)
        self.assertTrue(input_device.closed)
        self.assertTrue(uhid_device.destroyed)

    def test_capture_all_uses_one_device_and_preserves_order(self):
        scenario = oracle.Scenario(
            "ordered",
            (oracle.report(0x04), oracle.EMPTY_REPORT),
        )
        FakeInputDevice.read_batches = [
            (
                fake_event(oracle.EV_KEY, 30, 1),
                fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 0),
            ),
            (
                fake_event(oracle.EV_KEY, 30, 0),
                fake_event(oracle.EV_SYN, oracle.SYN_REPORT, 0),
            ),
        ]

        with mock.patch.object(
            oracle.select,
            "select",
            side_effect=lambda readers, _writers, _errors, _timeout: (
                (readers, [], [])
                if FakeInputDevice.instances[0].batches
                else ([], [], [])
            ),
        ):
            captures = oracle.capture_all((scenario,))

        self.assertEqual(len(FakeUhidDevice.instances), 1)
        self.assertEqual(captures[0].name, "ordered")
        self.assertEqual(
            tuple(step.report for step in captures[0].steps), scenario.reports
        )
        self.assertEqual(
            [
                event.value
                for step in captures[0].steps
                for event in step.events
                if event.type == oracle.EV_KEY
            ],
            [1, 0],
        )


class CliTests(unittest.TestCase):
    def test_oracle_error_is_a_one_line_diagnostic_with_exit_two(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(
                oracle, "capture_all", side_effect=oracle.OracleError("failed")
            ),
            redirect_stderr(stderr),
        ):
            exit_code = oracle.main([])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "boot keyboard oracle: failed\n")

    def test_default_output_is_repository_kernel_fixture(self):
        self.assertEqual(
            oracle.DEFAULT_OUTPUT,
            TOOL_DIR.parents[1] / "kernel/comps/usb/src/keyboard_linux_vectors.rs",
        )


if __name__ == "__main__":
    unittest.main()
