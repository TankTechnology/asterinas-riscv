#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Generates Linux evdev vectors for a USB HID Boot Keyboard."""

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import select
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


Report = tuple[int, int, int, int, int, int, int, int]
ERROR_USAGES = (1, 2, 3)
EV_SYN = 0
EV_KEY = 1
EV_MSC = 4
EV_REP = 20
SYN_REPORT = 0
MSC_SCAN = 4
REP_DELAY = 0
REP_PERIOD = 1

GENERATION_COMMAND = "python3 tools/usb-hid/boot_keyboard_oracle.py"
USB_HID_VERSION = "1.11"
HID_USAGE_TABLES_VERSION = "1.7"
DEVICE_DISCOVERY_TIMEOUT_SECONDS = 3.0
DEVICE_DISCOVERY_POLL_MILLISECONDS = 10
EVENT_QUIET_TIMEOUT_SECONDS = 0.2
SAFE_REPEAT_DELAY_MILLISECONDS = 60_000
SAFE_REPEAT_PERIOD_MILLISECONDS = 60_000
SAFE_REPEAT = (
    SAFE_REPEAT_DELAY_MILLISECONDS,
    SAFE_REPEAT_PERIOD_MILLISECONDS,
)
RUST_MAX_WIDTH = 100
RUST_ARRAY_WIDTH = 60

BOOT_KEYBOARD_DESCRIPTOR = bytes.fromhex(
    "05 01 09 06 A1 01 05 07 19 E0 29 E7 15 00 25 01 "
    "75 01 95 08 81 02 95 01 75 08 81 01 95 05 75 01 "
    "05 08 19 01 29 05 91 02 95 01 75 03 91 01 95 06 "
    "75 08 15 00 25 65 05 07 19 00 29 65 81 00 C0"
)

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[2]
    / "kernel/comps/usb/src/keyboard_linux_vectors.rs"
)


class OracleError(RuntimeError):
    """Indicates that the Linux oracle could not produce trustworthy data."""


def _byte(value: int, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer byte")
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{field} must be between 0 and 255")
    return value


def report(
    *usages: int,
    modifier: int = 0,
    reserved: int = 0,
) -> Report:
    """Builds one validated eight-byte Boot Keyboard input report."""

    if len(usages) > 6:
        raise ValueError("a Boot Keyboard report has at most six key slots")

    modifier = _byte(modifier, "modifier")
    reserved = _byte(reserved, "reserved")
    keys = tuple(_byte(usage, "usage") for usage in usages)
    padded_keys = keys + (0,) * (6 - len(keys))
    return (modifier, reserved, *padded_keys)


EMPTY_REPORT = report()


@dataclass(frozen=True)
class Scenario:
    """A named sequence of complete Boot Keyboard input reports."""

    name: str
    reports: tuple[Report, ...]


@dataclass(frozen=True)
class RawEvent:
    """One raw Linux input event."""

    type: int
    code: int
    value: int


@dataclass(frozen=True)
class StepCapture:
    """Normalized Linux events emitted for one HID report."""

    report: Report
    events: tuple[RawEvent, ...]


@dataclass(frozen=True)
class ScenarioCapture:
    """Captured Linux behavior for one scenario."""

    name: str
    steps: tuple[StepCapture, ...]


@dataclass(frozen=True)
class Provenance:
    """Versions and input hashes used for a generated fixture."""

    generation_command: str
    kernel_release: str
    hid_tools_version: str
    evdev_version: str
    usb_hid_version: str
    hid_usage_tables_version: str
    descriptor_sha256: str
    scenarios_sha256: str


def _usage_scenarios() -> tuple[Scenario, ...]:
    scenarios = []
    for usage in range(0x04, 0x66):
        pressed = report(usage)
        scenarios.append(
            Scenario(f"usage_{usage:02x}", (pressed, pressed, EMPTY_REPORT))
        )
    return tuple(scenarios)


def _modifier_scenarios() -> tuple[Scenario, ...]:
    scenarios = []
    for bit in range(8):
        pressed = report(modifier=1 << bit)
        scenarios.append(Scenario(f"modifier_{bit}", (pressed, pressed, EMPTY_REPORT)))

    # The A usage leaves Linux an event after two synthetic full-buffer flushes,
    # ensuring the report still has its terminal SYN_REPORT value zero.
    all_pressed = report(0x04, modifier=0xFF)
    scenarios.append(
        Scenario("all_modifiers", (all_pressed, all_pressed, EMPTY_REPORT))
    )
    return tuple(scenarios)


def _chord_scenarios() -> tuple[Scenario, ...]:
    scenarios = []
    for key_count in range(2, 7):
        # Four changed keys exactly fill Linux's estimated input value buffer.
        # Left Ctrl breaks that boundary so a real terminal sync is observable.
        modifier = 0x01 if key_count == 4 else 0
        pressed = report(
            *range(0x04, 0x04 + key_count),
            modifier=modifier,
        )
        scenarios.append(
            Scenario(f"chord_{key_count}", (pressed, pressed, EMPTY_REPORT))
        )
    return tuple(scenarios)


def _edge_case_scenarios() -> tuple[Scenario, ...]:
    a = report(0x04)
    b = report(0x05)
    ab = report(0x04, 0x05)
    abc = report(0x04, 0x05, 0x06)

    return (
        Scenario("zero_usage", (report(0), EMPTY_REPORT)),
        Scenario(
            "add_remove_modifier",
            (a, report(0x04, modifier=0x02), a, EMPTY_REPORT),
        ),
        Scenario(
            "simultaneous_modifier_release",
            (report(0x04, modifier=0xFF), EMPTY_REPORT),
        ),
        Scenario("shift_a", (report(0x04, modifier=0x02), EMPTY_REPORT)),
        Scenario("ctrl_alt_delete", (report(0x4C, modifier=0x05), EMPTY_REPORT)),
        Scenario(
            "six_key_partial_release",
            (
                report(0x04, 0x05, 0x06, 0x07, 0x08, 0x09),
                report(0x04, 0x06, 0x08),
                EMPTY_REPORT,
            ),
        ),
        Scenario("add_to_chord", (ab, abc, EMPTY_REPORT)),
        Scenario(
            "release_one_from_chord",
            (abc, report(0x04, 0x06), EMPTY_REPORT),
        ),
        # Ctrl breaks the four-key replacement's synthetic-flush boundary;
        # the modifier-only step then releases all three replacement keys.
        Scenario(
            "replace_subset",
            (
                abc,
                report(0x04, 0x07, 0x08, modifier=0x01),
                report(modifier=0x01),
                EMPTY_REPORT,
            ),
        ),
        Scenario("reordered_array", (ab, report(0x05, 0x04), EMPTY_REPORT)),
        Scenario("duplicate_usage", (report(0x04, 0x04), EMPTY_REPORT)),
        Scenario(
            "zero_filled_holes",
            (report(0x04, 0, 0x05, 0), EMPTY_REPORT),
        ),
        Scenario("replace_a_with_b", (a, b, EMPTY_REPORT)),
        Scenario(
            "backslash_alias_keycode_state",
            (
                report(0x31, 0x32),
                report(0x31),
                EMPTY_REPORT,
                report(0x31),
                report(0x31, 0x32),
                report(0x32),
                EMPTY_REPORT,
                report(0x31, 0x32),
                report(0x32, 0x31),
                EMPTY_REPORT,
            ),
        ),
        # Holding Ctrl proves the reserved-only change preserves key state. The
        # next report releases it before Linux's 250 ms EV_KEY repeat begins.
        Scenario(
            "reserved_byte_change",
            (
                report(modifier=0x01),
                report(modifier=0x01, reserved=0x7F),
                EMPTY_REPORT,
            ),
        ),
        Scenario("omitted_intermediate_report", (a, report(0x06), EMPTY_REPORT)),
        Scenario("unsupported_66", (report(0x66), EMPTY_REPORT)),
        Scenario("unsupported_ff", (report(0xFF), EMPTY_REPORT)),
    )


def _error_scenarios() -> tuple[Scenario, ...]:
    a = report(0x04)
    b = report(0x05)
    scenarios = []
    for error_usage in ERROR_USAGES:
        errors = report(*([error_usage] * 6))
        shifted_errors = report(*([error_usage] * 6), modifier=0x02)
        scenarios.extend(
            (
                Scenario(f"error_{error_usage}_empty", (errors, EMPTY_REPORT)),
                Scenario(
                    f"error_{error_usage}_held",
                    (a, errors, b, EMPTY_REPORT),
                ),
                Scenario(
                    f"error_{error_usage}_modifier",
                    (a, shifted_errors, a, EMPTY_REPORT),
                ),
            )
        )
    return tuple(scenarios)


SCENARIOS = (
    *_usage_scenarios(),
    *_modifier_scenarios(),
    *_chord_scenarios(),
    *_edge_case_scenarios(),
    *_error_scenarios(),
)


def normalize_events(events: tuple[RawEvent, ...]) -> tuple[RawEvent, ...]:
    """Removes non-semantic Linux metadata from one evdev report frame."""

    normalized = []
    has_key_event = False
    for event in events:
        if event.type == EV_MSC and event.code == MSC_SCAN:
            continue
        if event.type == EV_KEY:
            if event.value not in (0, 1):
                raise OracleError(
                    "unexpected evdev event "
                    f"(type={event.type}, code={event.code}, value={event.value})"
                )
            normalized.append(event)
            has_key_event = True
            continue
        if event.type == EV_SYN and event.code == SYN_REPORT:
            if event.value == 1:
                continue
            if event.value == 0:
                if has_key_event:
                    normalized.append(event)
                continue
        raise OracleError(
            "unexpected evdev event "
            f"(type={event.type}, code={event.code}, value={event.value})"
        )
    return tuple(normalized)


def _render_report(report_bytes: Report) -> str:
    return "[" + ", ".join(f"0x{byte:02x}" for byte in report_bytes) + "]"


def _render_event_lines(
    events: tuple[RawEvent, ...],
    indentation: str,
) -> tuple[str, ...]:
    triplets = ", ".join(
        f"({event.type}, {event.code}, {event.value})" for event in events
    )
    event_slice = f"&[{triplets}]"
    inline = f"{indentation}events: {event_slice},"
    if len(inline) <= RUST_MAX_WIDTH and len(event_slice) <= RUST_ARRAY_WIDTH:
        return (inline,)

    lines = [f"{indentation}events: &["]
    lines.extend(
        f"{indentation}    ({event.type}, {event.code}, {event.value}),"
        for event in events
    )
    lines.append(f"{indentation}],")
    return tuple(lines)


def render_rust(
    captures: tuple[ScenarioCapture, ...],
    provenance: Provenance,
) -> str:
    """Renders captures as deterministic private Rust test data."""

    lines = [
        "// SPDX-License-Identifier: MPL-2.0",
        "// @generated; do not edit.",
        f"// Generation command: {provenance.generation_command}",
        f"// Linux kernel: {provenance.kernel_release}",
        f"// hid-tools: {provenance.hid_tools_version}",
        f"// evdev: {provenance.evdev_version}",
        f"// USB HID: {provenance.usb_hid_version}",
        f"// HID Usage Tables: {provenance.hid_usage_tables_version}",
        f"// Descriptor SHA256: {provenance.descriptor_sha256}",
        f"// Scenarios SHA256: {provenance.scenarios_sha256}",
        "",
        "pub(super) struct LinuxStep {",
        "    pub(super) report: [u8; 8],",
        "    pub(super) events: &'static [(u16, u16, i32)],",
        "}",
        "",
        "pub(super) struct LinuxScenario {",
        "    pub(super) name: &'static str,",
        "    pub(super) steps: &'static [LinuxStep],",
        "}",
        "",
        "pub(super) static LINUX_SCENARIOS: &[LinuxScenario] = &[",
    ]
    for capture in captures:
        lines.extend(
            (
                "    LinuxScenario {",
                f"        name: {json.dumps(capture.name, ensure_ascii=False)},",
                "        steps: &[",
            )
        )
        for step in capture.steps:
            lines.extend(
                (
                    "            LinuxStep {",
                    f"                report: {_render_report(step.report)},",
                )
            )
            lines.extend(_render_event_lines(step.events, "                "))
            lines.append("            },")
        lines.extend(("        ],", "    },"))
    lines.append("];")
    return "\n".join(lines) + "\n"


def scenario_sha256(scenarios: tuple[Scenario, ...]) -> str:
    """Hashes the canonical scenario names and report byte arrays."""

    canonical_scenarios = [
        {
            "name": scenario.name,
            "reports": [list(report_bytes) for report_bytes in scenario.reports],
        }
        for scenario in scenarios
    ]
    serialized = json.dumps(
        canonical_scenarios,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def make_provenance() -> Provenance:
    """Collects the stable inputs and host versions for this invocation."""

    return Provenance(
        generation_command=GENERATION_COMMAND,
        kernel_release=platform.release(),
        hid_tools_version=importlib.metadata.version("hid-tools"),
        evdev_version=importlib.metadata.version("evdev"),
        usb_hid_version=USB_HID_VERSION,
        hid_usage_tables_version=HID_USAGE_TABLES_VERSION,
        descriptor_sha256=hashlib.sha256(BOOT_KEYBOARD_DESCRIPTOR).hexdigest(),
        scenarios_sha256=scenario_sha256(SCENARIOS),
    )


def publish(output: Path, content: str, check: bool) -> None:
    """Checks or atomically updates a generated UTF-8 file."""

    output = Path(output)
    if check:
        try:
            with output.open("r", encoding="utf-8", newline="") as output_file:
                existing = output_file.read()
        except (OSError, UnicodeError) as error:
            raise OracleError(f"cannot check {output}: {error}") from error
        if existing != content:
            raise OracleError(f"generated output differs from {output}")
        return

    temp_path = None
    temp_fd = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
        )
        temp_path = Path(temp_name)
        with os.fdopen(
            temp_fd,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as temp_file:
            temp_fd = None
            os.fchmod(temp_file.fileno(), 0o644)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, output)
        temp_path = None
    except (OSError, UnicodeError) as error:
        raise OracleError(f"cannot publish {output}: {error}") from error
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _load_linux_modules():
    """Loads maintenance-only Linux HID dependencies on demand."""

    return (
        importlib.import_module("hidtools.uhid"),
        importlib.import_module("evdev"),
    )


class LinuxUhidOracle:
    """Captures Linux evdev behavior from one temporary UHID keyboard."""

    def __init__(self):
        self.uhid_device = None
        self.input_device = None
        self._is_grabbed = False

    def __enter__(self):
        if not os.access("/dev/uhid", os.R_OK | os.W_OK):
            raise OracleError("/dev/uhid is not readable and writable")

        try:
            uhid_module, evdev_module = _load_linux_modules()
            self.uhid_device = uhid_module.UHIDDevice()
            self.uhid_device.name = f"asterinas-boot-keyboard-{self.uhid_device.uniq}"
            self.uhid_device.phys = "asterinas/usb-hid-oracle"
            self.uhid_device.info = (uhid_module.BusType.USB, 0x1D6B, 0xA57E)
            self.uhid_device.rdesc = BOOT_KEYBOARD_DESCRIPTOR
            self.uhid_device.create_kernel_device()

            node = self._wait_for_device_node()
            self.input_device = evdev_module.InputDevice(node)
            self.input_device.grab()
            self._is_grabbed = True
            self.input_device.repeat = SAFE_REPEAT
            actual_repeat = tuple(self.input_device.repeat)
            if actual_repeat != SAFE_REPEAT:
                raise OracleError(
                    "Linux evdev did not accept the safe keyboard repeat settings: "
                    f"expected {SAFE_REPEAT}, got {actual_repeat}"
                )
            self._drain_repeat_configuration_frame()
            return self
        except OracleError:
            self._cleanup_after_failed_enter()
            raise
        except Exception as error:
            self._cleanup_after_failed_enter()
            raise OracleError(f"cannot create Linux UHID oracle: {error}") from error

    def __exit__(self, exception_type, exception, traceback):
        try:
            self._cleanup()
        except OracleError:
            if exception is None:
                raise
        return False

    def _wait_for_device_node(self) -> str:
        deadline = time.monotonic() + DEVICE_DISCOVERY_TIMEOUT_SECONDS
        while True:
            self.uhid_device.dispatch(DEVICE_DISCOVERY_POLL_MILLISECONDS)
            nodes = list(self.uhid_device.device_nodes)
            if len(nodes) > 1:
                raise OracleError(
                    f"UHID keyboard created multiple evdev nodes: {nodes}"
                )
            if len(nodes) == 1:
                return nodes[0]
            if time.monotonic() >= deadline:
                raise OracleError(
                    "UHID keyboard did not create an evdev node within 3s"
                )

    def _drain_repeat_configuration_frame(self) -> None:
        readable, _, _ = select.select(
            [self.input_device],
            [],
            [],
            EVENT_QUIET_TIMEOUT_SECONDS,
        )
        if not readable:
            raise OracleError("keyboard repeat configuration timeout")

        actual_events = tuple(
            RawEvent(input_event.type, input_event.code, input_event.value)
            for input_event in self.input_device.read()
        )
        expected_events = (
            RawEvent(EV_REP, REP_DELAY, SAFE_REPEAT_DELAY_MILLISECONDS),
            RawEvent(EV_REP, REP_PERIOD, SAFE_REPEAT_PERIOD_MILLISECONDS),
            RawEvent(EV_SYN, SYN_REPORT, 0),
        )
        if actual_events != expected_events:
            raise OracleError(
                "unexpected keyboard repeat configuration frame: "
                f"expected {expected_events}, got {actual_events}"
            )

    def _cleanup_after_failed_enter(self) -> None:
        try:
            self._cleanup()
        except OracleError:
            pass

    def _cleanup(self) -> None:
        errors = []
        input_device = self.input_device
        self.input_device = None
        if input_device is not None:
            if self._is_grabbed:
                try:
                    input_device.ungrab()
                except Exception as error:
                    errors.append(f"cannot ungrab evdev device: {error}")
                self._is_grabbed = False
            try:
                input_device.close()
            except Exception as error:
                errors.append(f"cannot close evdev device: {error}")

        uhid_device = self.uhid_device
        self.uhid_device = None
        if uhid_device is not None:
            try:
                uhid_device.destroy()
            except Exception as error:
                errors.append(f"cannot destroy UHID device: {error}")

        if errors:
            raise OracleError("; ".join(errors))

    def capture_report(self, report_bytes: Report) -> tuple[RawEvent, ...]:
        """Sends one report and captures its normalized evdev frame."""

        if self.uhid_device is None or self.input_device is None:
            raise OracleError("Linux UHID oracle is not active")

        try:
            deadline = time.monotonic() + EVENT_QUIET_TIMEOUT_SECONDS
            self.uhid_device.call_input_event(report_bytes)
            captured = []
            while True:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    if captured:
                        raise OracleError(
                            "evdev quiet timeout after a partial input frame"
                        )
                    raise OracleError(
                        "evdev capture deadline expired before quiet timeout"
                    )

                readable, _, _ = select.select(
                    [self.input_device],
                    [],
                    [],
                    remaining_seconds,
                )
                if not readable:
                    if captured:
                        raise OracleError(
                            "evdev quiet timeout after a partial input frame"
                        )
                    return ()

                input_events = tuple(
                    RawEvent(
                        input_event.type,
                        input_event.code,
                        input_event.value,
                    )
                    for input_event in self.input_device.read()
                )
                for event_index, event in enumerate(input_events):
                    captured.append(event)
                    if (
                        event.type == EV_SYN
                        and event.code == SYN_REPORT
                        and event.value == 0
                    ):
                        if event_index != len(input_events) - 1:
                            raise OracleError(
                                "evdev read batch contains events after its "
                                "terminal SYN_REPORT"
                            )
                        return normalize_events(tuple(captured))
        except OracleError:
            raise
        except Exception as error:
            raise OracleError(f"cannot capture evdev input frame: {error}") from error


def capture_all(
    scenarios: tuple[Scenario, ...] = SCENARIOS,
) -> tuple[ScenarioCapture, ...]:
    """Captures every scenario in order with one Linux UHID device."""

    for scenario in scenarios:
        if not scenario.reports or scenario.reports[-1] != EMPTY_REPORT:
            raise OracleError(f"scenario {scenario.name!r} does not end empty")

    captures = []
    with LinuxUhidOracle() as linux_oracle:
        for scenario in scenarios:
            steps = tuple(
                StepCapture(report_bytes, linux_oracle.capture_report(report_bytes))
                for report_bytes in scenario.reports
            )
            captures.append(ScenarioCapture(scenario.name, steps))
    return tuple(captures)


def main(argv=None) -> int:
    """Runs the Linux oracle generator or checks the committed fixture."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args(argv)

    try:
        captures = capture_all()
        content = render_rust(captures, make_provenance())
        publish(arguments.output, content, arguments.check)
    except OracleError as error:
        diagnostic = " ".join(str(error).splitlines())
        print(f"boot keyboard oracle: {diagnostic}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
