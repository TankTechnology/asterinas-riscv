#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the QEMU physical-graphics interaction adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.riscv.debian.rootfs.debug_console_qemu_gate import (
    DEBUG_CONSOLE_QEMU_MILESTONES,
    DebugConsoleQemuOperations,
)
from tools.riscv.debian.rootfs.desktop_m5_network_gate import NETWORK_LAYERS
from tools.riscv.debian.rootfs.gate_protocol import GENERIC_SV39_CPU
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure
from tools.riscv.physical_graphics_qemu_gate import (
    QEMU_SCREEN_HEIGHT,
    QEMU_SCREEN_WIDTH,
    PhysicalGraphicsQemuOperations,
    QemuCycleArtifact,
    classify_physical_graphics_qemu,
    physical_graphics_qemu_argv,
    qemu_input_commands,
)


NONCES = (
    "0123456789abcdef",
    "fedcba9876543210",
    "0011223344556677",
)


def interaction_markers(*, absolute_events: int = 2) -> bytes:
    lines: list[str] = []
    for cycle, nonce in enumerate(NONCES, start=1):
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        event_hash = hashlib.sha256(f"event-{cycle}".encode()).hexdigest()
        screenshot_hash = hashlib.sha256(f"screenshot-{cycle}".encode()).hexdigest()
        lines.extend(
            (
                f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} "
                f"nonce_sha256={nonce_hash}",
                f"ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle={cycle} "
                f"key_downs=16 relative_events=0 "
                f"absolute_events={absolute_events} left_down=1 left_up=1 "
                f"digest={event_hash}",
                f"ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle={cycle} "
                f"nonce_sha256={nonce_hash} trusted_key=1 trusted_input=1 "
                "trusted_pointer=1 trusted_click=1 click_count=1 color=cyan",
                f"ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle={cycle} "
                f"sha256={screenshot_hash}",
                f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}",
            )
        )
    lines.append("ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=3")
    return ("\n".join(lines) + "\n").encode()


def passing_transcript() -> bytes:
    lines = [
        *DEBUG_CONSOLE_QEMU_MILESTONES,
        *(
            f"DEBIAN_WEB_NETWORK_LAYER mode=direct layer={layer} status=pass"
            for layer in NETWORK_LAYERS
        ),
        "BROWSER_WEB_DESKTOP_STAGE=x-socket-ready guest_monotonic_ns=1 pid=2",
        f"DEBIAN_WEB_NETWORK_READY mode=direct layers={len(NETWORK_LAYERS)}",
    ]
    return ("\n".join(lines) + "\n").encode() + interaction_markers()


class PhysicalGraphicsQemuArgvTests(unittest.TestCase):
    def test_argv_is_the_frozen_four_hart_graphical_network_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [root / name for name in ("u-boot", "boot.ext4", "root.ext2")]
            for path in files:
                path.write_bytes(b"fixture")
            monitor = root / "monitor.sock"
            argv = physical_graphics_qemu_argv(
                uboot=files[0],
                boot_disk=files[1],
                root_disk=files[2],
                monitor_socket=monitor,
                smp=4,
                dtb_enabled_cpu_count=4,
            )

        self.assertEqual(argv[argv.index("-cpu") + 1], GENERIC_SV39_CPU)
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertEqual(argv[argv.index("-display") + 1], "none")
        self.assertEqual(argv.count("bochs-display"), 1)
        self.assertEqual(argv.count("virtio-keyboard-device"), 1)
        self.assertEqual(argv.count("virtio-tablet-device"), 1)
        self.assertEqual(sum(value == "-drive" for value in argv), 2)
        self.assertEqual(argv.count("user,id=net0"), 1)
        self.assertEqual(argv.count("virtio-net-device,netdev=net0"), 1)
        self.assertEqual(
            argv[argv.index("-monitor") + 1],
            f"unix:{monitor},server=on,wait=off",
        )
        flattened = " ".join(argv).lower()
        self.assertNotIn("-vnc", flattened)
        self.assertNotIn("gtk", flattened)

    def test_bootargs_keep_debug_root_but_mask_the_competing_browser_gate(self) -> None:
        bootargs = PhysicalGraphicsQemuOperations.BOOTARGS
        self.assertTrue(
            bootargs.endswith("-- --root-init=systemd --debug-console=root")
        )
        self.assertIn("systemd.mask=asterinas-browser-web-evidence.service", bootargs)
        self.assertIn("asterinas.debian_network=qemu-slirp", bootargs)
        self.assertFalse(PhysicalGraphicsQemuOperations.CAPTURE_DEBUG_SCREENSHOT)


class PhysicalGraphicsQemuInputTests(unittest.TestCase):
    def test_commands_are_exactly_sixteen_hex_keys_then_relative_move_and_click(
        self,
    ) -> None:
        commands = qemu_input_commands(NONCES[0])
        self.assertEqual(len(commands), 20)
        self.assertEqual(
            tuple(command.split()[1] for command in commands[:16]),
            tuple(NONCES[0]),
        )
        self.assertTrue(
            all(command.startswith("sendkey ") for command in commands[:16])
        )
        self.assertEqual(commands[16], "mouse_move -32767 -32767")
        self.assertEqual(commands[17], "mouse_move 640 600")
        self.assertEqual(commands[18:], ("mouse_button 1", "mouse_button 0"))
        joined = " ".join(commands)
        for forbidden in ("xdotool", "Marionette", "PerformActions", "-vnc", "gtk"):
            self.assertNotIn(forbidden, joined)

    def test_rejects_noncanonical_nonce(self) -> None:
        for nonce in ("", "0" * 15, "0" * 17, "A" * 16, "0" * 15 + "g"):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                qemu_input_commands(nonce)

    def test_one_cycle_waits_for_ready_before_any_hmp_input_and_captures_after_pass(
        self,
    ) -> None:
        events: list[str] = []

        class Serial:
            transcript = b"cycle transcript"

            def checkpoint(self) -> int:
                return 7

            def send(self, payload: bytes, deadline: float) -> None:
                del deadline
                text = payload.decode()
                events.append("guest-command")
                self.command = text

        class Monitor:
            def command(self, command: str, deadline: float) -> bytes:
                del deadline
                events.append(f"hmp:{command}")
                return b"(qemu) "

        serial = Serial()
        session = {
            "serial": serial,
            "monitor": Monitor(),
            "directory": Path("/tmp/physical-graphics-qemu-test"),
        }
        config = mock.Mock(command_timeout=30.0)
        operations = object.__new__(PhysicalGraphicsQemuOperations)
        operations._browser_pid = 42
        operations._cycle_artifacts = []
        nonce_hash = hashlib.sha256(NONCES[0].encode()).hexdigest()
        lines = iter(
            (
                f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=1 nonce_sha256={nonce_hash}",
                "ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle=1",
                "__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle=1 status=0",
            )
        )

        def next_line(
            _serial: object, cursor: int, _deadline: float
        ) -> tuple[str, int]:
            line = next(lines)
            if "_READY " in line:
                events.append("ready")
            elif "_PASS " in line:
                events.append("pass")
            elif "COMMAND_STATUS" in line:
                events.append("status")
            return line, cursor + 1

        with (
            mock.patch(
                "tools.riscv.physical_graphics_qemu_gate._next_line",
                side_effect=next_line,
            ),
            mock.patch(
                "tools.riscv.physical_graphics_qemu_gate.extract_screenshot_frame",
                return_value=b"guest-png",
            ),
            mock.patch(
                "tools.riscv.physical_graphics_qemu_gate.capture_rendered_ppm",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("capture") or (b"ppm", {"width": 1280})
                ),
            ),
            mock.patch(
                "tools.riscv.physical_graphics_qemu_gate.time.monotonic",
                return_value=100.0,
            ),
            mock.patch("tools.riscv.physical_graphics_qemu_gate.time.sleep"),
        ):
            evidence = operations._run_interaction_cycle(
                session, config, cycle=1, nonce=NONCES[0]
            )

        ready_index = events.index("ready")
        pass_index = events.index("pass")
        capture_index = events.index("capture")
        hmp_indices = [
            index for index, event in enumerate(events) if event.startswith("hmp:")
        ]
        self.assertEqual(len(hmp_indices), 20)
        self.assertTrue(all(index > ready_index for index in hmp_indices))
        self.assertTrue(all(index < pass_index for index in hmp_indices))
        self.assertGreater(capture_index, pass_index)
        self.assertIn("--expected-width 1280", serial.command)
        self.assertIn("--expected-height 1024", serial.command)
        self.assertNotIn("xdotool", serial.command)
        self.assertEqual(evidence.cycle, 1)

    def test_rejects_pass_buffered_with_ready_before_sending_any_hmp_input(
        self,
    ) -> None:
        nonce_hash = hashlib.sha256(NONCES[0].encode()).hexdigest()

        class Serial:
            def __init__(self) -> None:
                self.transcript = b""

            def checkpoint(self) -> int:
                return len(self.transcript)

            def send(self, payload: bytes, deadline: float) -> None:
                del payload, deadline
                self.transcript += (
                    f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=1 "
                    f"nonce_sha256={nonce_hash}\n"
                    "ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle=1\n"
                ).encode()

            def wait_for(
                self, marker: bytes, deadline: float, *, start: int = 0
            ) -> bytes:
                del marker, deadline, start
                return self.transcript

        monitor = mock.Mock()
        monitor.command.side_effect = GateFailure(
            "HMP called before buffered PASS rejection"
        )
        operations = object.__new__(PhysicalGraphicsQemuOperations)
        operations._browser_pid = 42
        operations._cycle_artifacts = []
        with self.assertRaisesRegex(GateFailure, "before HMP"):
            operations._run_interaction_cycle(
                {
                    "serial": Serial(),
                    "monitor": monitor,
                    "directory": Path("/tmp/physical-graphics-qemu-test"),
                },
                mock.Mock(command_timeout=30.0),
                cycle=1,
                nonce=NONCES[0],
            )
        monitor.command.assert_not_called()

    def test_protocol_runs_debug_then_three_distinct_cycles_and_final_check(
        self,
    ) -> None:
        operations = object.__new__(PhysicalGraphicsQemuOperations)
        operations._nonces = ()
        operations._browser_pid = None
        operations._cycle_artifacts = []
        operations._classified_cycles = ()
        operations._screenshot = b"baseline"
        operations._screenshot_metadata = {"width": QEMU_SCREEN_WIDTH}
        events: list[str] = []

        def run_cycle(
            _session: object,
            _config: object,
            *,
            cycle: int,
            nonce: str,
        ) -> QemuCycleArtifact:
            events.append(f"cycle-{cycle}:{nonce}")
            evidence = QemuCycleArtifact(
                cycle=cycle,
                nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest(),
                guest_png_sha256=hashlib.sha256(f"png-{cycle}".encode()).hexdigest(),
                rendered_ppm_sha256=hashlib.sha256(f"ppm-{cycle}".encode()).hexdigest(),
                rendered={"width": QEMU_SCREEN_WIDTH, "height": QEMU_SCREEN_HEIGHT},
            )
            operations._cycle_artifacts.append(
                (evidence, f"png-{cycle}".encode(), f"ppm-{cycle}".encode())
            )
            return evidence

        session = {"serial": mock.sentinel.serial}
        config = mock.Mock(command_timeout=30.0)
        with (
            mock.patch.object(
                DebugConsoleQemuOperations,
                "run_protocol",
                side_effect=lambda *_args: events.append("debug"),
            ),
            mock.patch.object(
                operations,
                "_query_browser_pid",
                side_effect=lambda *_args: events.append("browser") or 42,
            ),
            mock.patch.object(
                operations, "_run_interaction_cycle", side_effect=run_cycle
            ),
            mock.patch.object(
                operations,
                "_verify_final_state",
                side_effect=lambda *_args: events.append("final"),
            ),
            mock.patch.object(
                operations,
                "_emit_complete",
                side_effect=lambda *_args: events.append("complete"),
            ),
            mock.patch(
                "tools.riscv.physical_graphics_qemu_gate.secrets.token_hex",
                side_effect=NONCES,
            ),
            mock.patch(
                "tools.riscv.physical_graphics_qemu_gate.time.monotonic",
                return_value=100.0,
            ),
        ):
            operations.run_protocol(session, config)

        self.assertEqual(
            events,
            [
                "debug",
                "browser",
                *(f"cycle-{cycle}:{nonce}" for cycle, nonce in enumerate(NONCES, 1)),
                "final",
                "complete",
            ],
        )
        self.assertEqual(operations._nonces, NONCES)
        self.assertEqual(operations._browser_pid, 42)
        self.assertEqual(len(operations._cycle_artifacts), 3)
        self.assertEqual(operations._screenshot, b"")
        self.assertEqual(operations._screenshot_metadata, {})

    def test_publish_labels_failure_nonphysical_and_writes_cycles_before_result(
        self,
    ) -> None:
        events: list[str] = []

        class Output:
            def atomic_write(self, name: str, payload: bytes) -> None:
                events.append(f"write:{name}:{len(payload)}")

        operations = object.__new__(PhysicalGraphicsQemuOperations)
        artifact = QemuCycleArtifact(
            cycle=1,
            nonce_sha256="1" * 64,
            guest_png_sha256=hashlib.sha256(b"png").hexdigest(),
            rendered_ppm_sha256=hashlib.sha256(b"ppm").hexdigest(),
            rendered={"width": QEMU_SCREEN_WIDTH, "height": QEMU_SCREEN_HEIGHT},
        )
        operations._cycle_artifacts = [(artifact, b"png", b"ppm")]
        operations._classified_cycles = ()
        result: dict[str, object] = {"passed": False, "reason": "fixture"}
        with (
            mock.patch.object(operations, "_require_output", return_value=Output()),
            mock.patch.object(
                DebugConsoleQemuOperations,
                "publish",
                side_effect=lambda *_args: events.append("result"),
            ),
        ):
            operations.publish(
                mock.sentinel.config,
                mock.sentinel.prepared,
                b"transcript",
                result,
            )

        self.assertIs(result["physical"], False)
        self.assertEqual(len(result["cycle_artifacts"]), 1)
        self.assertEqual(
            events,
            [
                "write:physical-graphics-qemu-cycle-1.png:3",
                "write:physical-graphics-qemu-cycle-1.ppm:3",
                "result",
            ],
        )


class PhysicalGraphicsQemuClassifierTests(unittest.TestCase):
    def test_accepts_qemu_tablet_absolute_events_but_is_never_physical(self) -> None:
        result = classify_physical_graphics_qemu(
            passing_transcript(),
            expected_debian_release="13.6",
            nonces=NONCES,
        )
        self.assertTrue(result.passed, result.reason)
        self.assertFalse(result.physical)
        self.assertEqual(len(result.cycles), 3)
        self.assertTrue(all(cycle.absolute_events == 2 for cycle in result.cycles))

    def test_rejects_tablet_cycle_without_motion(self) -> None:
        result = classify_physical_graphics_qemu(
            passing_transcript().replace(b"absolute_events=2", b"absolute_events=0"),
            expected_debian_release="13.6",
            nonces=NONCES,
        )
        self.assertFalse(result.passed)

    def test_rejects_rel_only_motion_in_qemu_tablet_mode(self) -> None:
        transcript = passing_transcript().replace(
            b"relative_events=0 absolute_events=2",
            b"relative_events=2 absolute_events=0",
        )
        result = classify_physical_graphics_qemu(
            transcript,
            expected_debian_release="13.6",
            nonces=NONCES,
        )
        self.assertFalse(result.passed)

    def test_adapter_accepts_only_browser_web_rootfs(self) -> None:
        self.assertEqual(
            PhysicalGraphicsQemuOperations._accepted_profile_identities(),
            ((7, "browser-web"),),
        )

    def test_adapter_composes_debug_and_interaction_classifiers(self) -> None:
        operations = object.__new__(PhysicalGraphicsQemuOperations)
        operations._nonces = NONCES
        operations._validated_profile = "browser-web"
        result = operations.classify_transcript(
            passing_transcript(), expected_debian_release="13.6"
        )
        self.assertTrue(result.passed, result.reason)
        self.assertFalse(result.physical)

        with mock.patch.object(
            DebugConsoleQemuOperations,
            "classify_transcript",
            return_value=mock.Mock(passed=False, reason="debug failed"),
        ):
            result = operations.classify_transcript(
                passing_transcript(), expected_debian_release="13.6"
            )
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "debug failed")

    def test_missing_nonces_fails_closed(self) -> None:
        operations = object.__new__(PhysicalGraphicsQemuOperations)
        operations._nonces = ()
        operations._validated_profile = "browser-web"
        result = operations.classify_transcript(
            passing_transcript(), expected_debian_release="13.6"
        )
        self.assertFalse(result.passed)
        self.assertIn("nonces", result.reason)


class PhysicalGraphicsQemuConstantsTests(unittest.TestCase):
    def test_qemu_geometry_matches_the_existing_bochs_contract(self) -> None:
        self.assertEqual((QEMU_SCREEN_WIDTH, QEMU_SCREEN_HEIGHT), (1280, 1024))


if __name__ == "__main__":
    unittest.main()
