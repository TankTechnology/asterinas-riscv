#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs.debug_console_protocol import (
    MAX_DEBUG_CONSOLE_TRANSCRIPT_BYTES,
    DebugConsoleEvidence,
    DebugConsoleProtocolError,
    classify_debug_console,
    debug_console_commands,
    run_debug_console_phase,
)
from tools.riscv.debian.rootfs.debug_console_qemu_gate import (
    DEBUG_CONSOLE_QEMU_MILESTONES,
    DebugConsoleQemuOperations,
    classify_debug_console_qemu,
    orchestrate_debug_console_qemu_gate,
)
from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
    NETWORK_LAYERS,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import DesktopM5QemuOperations
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure


NONCE = "0123456789abcdef0123456789abcdef"
PASSING_OUTPUTS = {
    "uid": "0",
    "pid1": "systemd",
    "root": "/dev/mmcblk0p2 ext2",
    "graphical": "active",
    "desktop": "active",
}


def make_transcript(
    *,
    outputs: dict[str, str] | None = None,
    statuses: dict[str, int] | None = None,
) -> str:
    selected_outputs = PASSING_OUTPUTS if outputs is None else outputs
    selected_statuses = {} if statuses is None else statuses
    lines: list[str] = []
    for command in debug_console_commands(NONCE):
        lines.extend(
            (
                command.begin_marker,
                f"{command.value_prefix}{selected_outputs[command.name]}",
                f"{command.status_prefix}{selected_statuses.get(command.name, 0)}",
                command.end_marker,
            )
        )
    return "\n".join(lines) + "\n"


class ScriptedSerial:
    def __init__(self) -> None:
        self._transcript = bytearray(b"ASTERINAS_DEBUG_CONSOLE_READY uid=0\r\r\n")

    @property
    def transcript(self) -> bytes:
        return bytes(self._transcript)

    def checkpoint(self) -> int:
        return len(self._transcript)

    def send(self, payload: bytes, deadline: float) -> None:
        del deadline
        text = payload.decode().rstrip("\n")
        command = next(
            command
            for command in debug_console_commands(NONCE)
            if command.payload == text
        )
        self._transcript.extend(payload.rstrip(b"\n") + b"\r\r\n")
        for line in (
            command.begin_marker,
            f"{command.value_prefix}{PASSING_OUTPUTS[command.name]}",
            f"{command.status_prefix}0",
            command.end_marker,
        ):
            self._transcript.extend(line.encode() + b"\r\r\n")

    def wait_for(self, marker: bytes, deadline: float, *, start: int = 0) -> bytes:
        del deadline
        if self.transcript.find(marker, start) < 0:
            raise TimeoutError(marker)
        return self.transcript

    def wait_for_any(
        self, markers: tuple[bytes, ...], deadline: float, *, start: int = 0
    ) -> bytes:
        del deadline
        for marker in markers:
            if self.transcript.find(marker, start) >= 0:
                return marker
        raise TimeoutError(markers)


class DebugConsoleProtocolTests(unittest.TestCase):
    def test_passing_transcript_yields_exact_evidence(self) -> None:
        self.assertEqual(
            classify_debug_console(make_transcript(), NONCE),
            DebugConsoleEvidence(
                uid=0,
                pid1="systemd",
                root_device="/dev/mmcblk0p2",
                root_filesystem="ext2",
                graphical_state="active",
                desktop_state="active",
            ),
        )

    def test_commands_are_five_fixed_read_only_probes(self) -> None:
        commands = debug_console_commands(NONCE)
        self.assertEqual(
            tuple(command.name for command in commands),
            ("uid", "pid1", "root", "graphical", "desktop"),
        )
        self.assertEqual(len({command.payload for command in commands}), 5)
        self.assertTrue(all(NONCE in command.payload for command in commands))
        self.assertIn("asterinas-desktop-m4.service", commands[-1].payload)
        self.assertIn("asterinas-desktop-m5.service", commands[-1].payload)
        self.assertIn("asterinas-desktop-m4-evidence.service", commands[-2].payload)
        self.assertIn("asterinas-desktop-m5.service", commands[-2].payload)
        self.assertIn("while ! systemctl", commands[-2].payload)
        self.assertTrue(
            all("_asterinas_debug_output=$(" in command.payload for command in commands)
        )
        self.assertTrue(
            all(command.payload.count("printf ") == 1 for command in commands)
        )

    def test_runtime_accepts_tty_cr_cr_lf_line_endings(self) -> None:
        evidence = run_debug_console_phase(
            ScriptedSerial(), 123.0, NONCE, ready_seen=False
        )
        self.assertEqual(evidence.uid, 0)
        self.assertEqual(evidence.desktop_state, "active")

    def assert_output_rejected(self, name: str, value: str) -> None:
        outputs = dict(PASSING_OUTPUTS)
        outputs[name] = value
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(make_transcript(outputs=outputs), NONCE)

    def test_rejects_non_root_uid(self) -> None:
        self.assert_output_rejected("uid", "1000")

    def test_rejects_non_systemd_pid1(self) -> None:
        self.assert_output_rejected("pid1", "bash")

    def test_rejects_wrong_megrez_root_partition(self) -> None:
        self.assert_output_rejected("root", "/dev/mmcblk0p3 ext2")

    def test_accepts_qemu_virtio_root_partition(self) -> None:
        outputs = dict(PASSING_OUTPUTS)
        outputs["root"] = "/dev/vdb ext2"
        evidence = classify_debug_console(make_transcript(outputs=outputs), NONCE)
        self.assertEqual(evidence.root_device, "/dev/vdb")

    def test_rejects_non_ext2_root(self) -> None:
        self.assert_output_rejected("root", "/dev/mmcblk0p2 ext4")

    def test_rejects_inactive_graphical_target(self) -> None:
        self.assert_output_rejected("graphical", "inactive")

    def test_rejects_inactive_desktop_service(self) -> None:
        self.assert_output_rejected("desktop", "failed")

    def test_rejects_nonzero_command_status(self) -> None:
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(make_transcript(statuses={"graphical": 3}), NONCE)

    def test_rejects_duplicate_marker(self) -> None:
        transcript = make_transcript()
        marker = debug_console_commands(NONCE)[0].begin_marker
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(f"{marker}\n{transcript}", NONCE)

    def test_rejects_reordered_markers(self) -> None:
        commands = debug_console_commands(NONCE)
        first, second = commands[:2]
        transcript = (
            make_transcript()
            .replace(first.begin_marker, second.begin_marker, 1)
            .replace(first.end_marker, second.end_marker, 1)
        )
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(transcript, NONCE)

    def test_rejects_stale_nonce_marker(self) -> None:
        stale = "fedcba9876543210fedcba9876543210"
        transcript = make_transcript() + debug_console_commands(stale)[0].begin_marker
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(transcript, NONCE)

    def test_rejects_ansi_or_osc_control_input(self) -> None:
        for sequence in ("\x1b[31m", "\x1b]0;spoofed\x07"):
            with self.subTest(sequence=repr(sequence)):
                outputs = dict(PASSING_OUTPUTS)
                outputs["uid"] = sequence + "0"
                with self.assertRaises(DebugConsoleProtocolError):
                    classify_debug_console(make_transcript(outputs=outputs), NONCE)

    def test_allows_unframed_ansi_boot_logs(self) -> None:
        transcript = "\x1b[32mReached target\x1b[0m\n" + make_transcript()
        self.assertEqual(classify_debug_console(transcript, NONCE).uid, 0)

    def test_allows_async_kernel_logs_between_protocol_lines(self) -> None:
        command = debug_console_commands(NONCE)[2]
        transcript = (
            make_transcript()
            .replace(
                command.begin_marker,
                command.begin_marker
                + "\n\x1b[32m[ 13.507] ERROR: block write is unsupported\x1b[0m",
                1,
            )
            .replace(
                f"{command.value_prefix}{PASSING_OUTPUTS[command.name]}",
                f"{command.value_prefix}{PASSING_OUTPUTS[command.name]}"
                + "\n\x1b[33m[ 13.508] WARN: asynchronous printk\x1b[0m",
                1,
            )
        )

        self.assertEqual(classify_debug_console(transcript, NONCE).uid, 0)

    def test_rejects_transcript_over_eight_mib(self) -> None:
        oversized = b"x" * (MAX_DEBUG_CONSOLE_TRANSCRIPT_BYTES + 1)
        with self.assertRaises(DebugConsoleProtocolError):
            classify_debug_console(oversized, NONCE)

    def test_rejects_invalid_nonce(self) -> None:
        for nonce in ("", "A" * 32, "0" * 31, "0" * 33, "0" * 31 + "\n"):
            with self.subTest(nonce=nonce):
                with self.assertRaises(ValueError):
                    debug_console_commands(nonce)

    def test_rejects_mutated_command_contract(self) -> None:
        command = debug_console_commands(NONCE)[0]
        mutated = replace(command, payload="id")
        self.assertNotEqual(mutated, command)


class DebugConsoleQemuAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.operations = object.__new__(DebugConsoleQemuOperations)
        self.operations._validated_profile = "browser-web"
        self.evidence = DebugConsoleEvidence(
            uid=0,
            pid1="systemd",
            root_device="/dev/vdb",
            root_filesystem="ext2",
            graphical_state="active",
            desktop_state="active",
        )

    def test_bootargs_append_debug_selector_after_systemd(self) -> None:
        self.assertTrue(
            self.operations.BOOTARGS.endswith(
                "-- --root-init=systemd --debug-console=root"
            )
        )
        self.assertEqual(self.operations.BOOTARGS.split().count("loglevel=off"), 1)
        self.assertEqual(
            [
                token
                for token in self.operations.BOOTARGS.split()
                if token.startswith("loglevel=")
            ],
            ["loglevel=off"],
        )
        self.assertEqual(
            self.operations.BOOTARGS.split().count(
                "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=direct"
            ),
            1,
        )
        self.assertEqual(
            self.operations.BOOTARGS.split().count(
                "systemd.setenv=ASTERINAS_DESKTOP_M5_NETWORK_MODE=lightweight"
            ),
            1,
        )

    def test_adapter_tracks_the_complete_debug_lifecycle(self) -> None:
        self.assertEqual(
            self.operations.MILESTONES,
            DEBUG_CONSOLE_QEMU_MILESTONES,
        )
        self.assertEqual(
            self.operations.FAILURE_MARKER,
            b"DEBIAN_ROOTFS_FAIL reason=",
        )
        self.assertFalse(self.operations.CAPTURE_SCREENSHOT)

    def test_accepts_m5_and_browser_web_manifests(self) -> None:
        self.assertEqual(
            self.operations._accepted_profile_identities(),
            ((5, "desktop-m5-network"), (7, "browser-web")),
        )

    def test_validate_inputs_binds_the_classifier_to_the_manifest_profile(self) -> None:
        with mock.patch.object(
            DesktopM5QemuOperations,
            "validate_inputs",
            return_value={"profile": "desktop-m5-network"},
        ):
            identity = self.operations.validate_inputs(
                mock.sentinel.config, mock.sentinel.snapshots
            )

        self.assertEqual(identity["profile"], "desktop-m5-network")
        self.assertEqual(self.operations._validated_profile, "desktop-m5-network")

    def test_runs_base_boot_before_debug_probes_and_then_captures(self) -> None:
        serial = mock.Mock()
        session = {
            "serial": serial,
            "monitor": mock.sentinel.monitor,
            "directory": Path("/tmp/debug-console-test"),
        }
        config = mock.Mock(command_timeout=17.0, boot_timeout=83.0)
        calls: list[str] = []
        readiness = iter(
            (
                ("desktop", b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready"),
                ("network", b"DEBIAN_WEB_NETWORK_READY mode=direct layers=10"),
            )
        )

        def wait_for_any(*_args: object, **_kwargs: object) -> bytes:
            name, marker = next(readiness)
            calls.append(name)
            return marker

        serial.wait_for_any.side_effect = wait_for_any
        with (
            mock.patch.object(
                DesktopM5QemuOperations,
                "run_protocol",
                side_effect=lambda *_: calls.append("base"),
            ) as base_protocol,
            mock.patch(
                "tools.riscv.debian.rootfs.debug_console_qemu_gate."
                "run_debug_console_phase",
                side_effect=lambda *_args, **_kwargs: (
                    calls.append("debug") or self.evidence
                ),
            ) as debug_protocol,
            mock.patch(
                "tools.riscv.debian.rootfs.debug_console_qemu_gate.secrets.token_hex",
                return_value=NONCE,
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.debug_console_qemu_gate.time.monotonic",
                return_value=100.0,
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.debug_console_qemu_gate."
                "capture_rendered_ppm",
                side_effect=lambda *_args, **_kwargs: (
                    calls.append("capture") or (b"ppm", {"width": 1280})
                ),
            ) as capture,
        ):
            self.operations.run_protocol(session, config)

        self.assertEqual(calls, ["base", "desktop", "network", "debug", "capture"])
        base_protocol.assert_called_once_with(session, config)
        self.assertEqual(serial.wait_for_any.call_count, 2)
        debug_protocol.assert_called_once_with(
            session["serial"], 117.0, NONCE, ready_seen=True
        )
        capture.assert_called_once_with(
            session["monitor"],
            Path("/tmp/debug-console-test/debug-root-console.ppm"),
            117.0,
        )
        self.assertEqual(self.operations.debug_evidence, self.evidence)
        self.assertEqual(self.operations._screenshot, b"ppm")
        self.assertEqual(self.operations._screenshot_metadata, {"width": 1280})

    def test_schema_five_waits_only_for_the_complete_m5_contract(self) -> None:
        self.operations._validated_profile = "desktop-m5-network"
        serial = mock.Mock()
        serial.wait_for_any.return_value = DESKTOP_M4_MILESTONES[-1].encode()
        session = {
            "serial": serial,
            "monitor": mock.sentinel.monitor,
            "directory": Path("/tmp/debug-console-test"),
        }
        config = mock.Mock(command_timeout=17.0, boot_timeout=83.0)
        with (
            mock.patch.object(DesktopM5QemuOperations, "run_protocol"),
            mock.patch(
                "tools.riscv.debian.rootfs.debug_console_qemu_gate."
                "run_debug_console_phase",
                return_value=self.evidence,
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.debug_console_qemu_gate."
                "capture_rendered_ppm",
                return_value=(b"ppm", {"width": 1280}),
            ),
            mock.patch(
                "tools.riscv.debian.rootfs.debug_console_qemu_gate.time.monotonic",
                return_value=100.0,
            ),
        ):
            self.operations.run_protocol(session, config)

        serial.wait_for_any.assert_called_once()
        self.assertEqual(
            serial.wait_for_any.call_args.args[0][0],
            DESKTOP_M4_MILESTONES[-1].encode(),
        )

    def test_debug_protocol_failure_becomes_gate_failure(self) -> None:
        serial = mock.Mock()
        session = {"serial": serial}
        config = mock.Mock(command_timeout=17.0, boot_timeout=83.0)
        with (
            mock.patch.object(DesktopM5QemuOperations, "run_protocol"),
            mock.patch(
                "tools.riscv.debian.rootfs.debug_console_qemu_gate."
                "run_debug_console_phase",
                side_effect=DebugConsoleProtocolError("wrong uid"),
            ),
        ):
            with self.assertRaisesRegex(GateFailure, "wrong uid"):
                self.operations.run_protocol(session, config)

    def test_publish_adds_structured_debug_evidence(self) -> None:
        self.operations.debug_evidence = self.evidence
        result: dict[str, object] = {}
        with mock.patch.object(DesktopM5QemuOperations, "publish") as publish:
            self.operations.publish(mock.sentinel.config, None, b"serial", result)

        self.assertEqual(
            result["debug_console"],
            {
                "uid": 0,
                "pid1": "systemd",
                "root_device": "/dev/vdb",
                "root_filesystem": "ext2",
                "graphical_state": "active",
                "desktop_state": "active",
            },
        )
        publish.assert_called_once_with(mock.sentinel.config, None, b"serial", result)


class DebugConsoleQemuClassifierTests(unittest.TestCase):
    def passing_web_transcript(self) -> bytes:
        lines = [
            *DEBUG_CONSOLE_QEMU_MILESTONES,
            *(
                f"DEBIAN_WEB_NETWORK_LAYER mode=direct layer={layer} status=pass"
                for layer in NETWORK_LAYERS
            ),
            "BROWSER_WEB_DESKTOP_STAGE=x-socket-ready guest_monotonic_ns=1 pid=2",
            f"DEBIAN_WEB_NETWORK_READY mode=direct layers={len(NETWORK_LAYERS)}",
        ]
        return ("\n".join(lines) + "\n").encode()

    def passing_m5_transcript(self) -> bytes:
        return (
            "\n".join(
                (
                    *DEBUG_CONSOLE_QEMU_MILESTONES,
                    *DESKTOP_M5_QEMU_MILESTONES,
                    *DESKTOP_M4_MILESTONES,
                )
            )
            + "\n"
        ).encode()

    def test_accepts_one_ordered_root_console_lifecycle(self) -> None:
        result = classify_debug_console_qemu(
            self.passing_web_transcript(),
            expected_debian_release="13.6",
            expected_profile="browser-web",
        )

        self.assertTrue(result.passed, result.reason)

    def test_accepts_existing_schema_five_desktop_contract(self) -> None:
        result = classify_debug_console_qemu(
            self.passing_m5_transcript(),
            expected_debian_release="13.6",
            expected_profile="desktop-m5-network",
        )

        self.assertTrue(result.passed, result.reason)

    def test_rejects_incomplete_web_network_or_desktop_startup(self) -> None:
        passing = self.passing_web_transcript()
        cases = (
            passing.replace(
                b"DEBIAN_WEB_NETWORK_LAYER mode=direct layer=https status=pass\n",
                b"",
                1,
            ),
            passing.replace(b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready", b"", 1),
        )
        for transcript in cases:
            with self.subTest(transcript=transcript):
                result = classify_debug_console_qemu(
                    transcript,
                    expected_debian_release="13.6",
                    expected_profile="browser-web",
                )
                self.assertFalse(result.passed)

    def test_rejects_missing_duplicate_or_reordered_milestones(self) -> None:
        passing = self.passing_web_transcript()
        cases = (
            passing.replace((DEBUG_CONSOLE_QEMU_MILESTONES[0] + "\n").encode(), b"", 1),
            passing + (DEBUG_CONSOLE_QEMU_MILESTONES[-1] + "\n").encode(),
            ("\n".join(reversed(DEBUG_CONSOLE_QEMU_MILESTONES)) + "\n").encode(),
        )
        for transcript in cases:
            with self.subTest(transcript=transcript):
                result = classify_debug_console_qemu(
                    transcript,
                    expected_debian_release="13.6",
                    expected_profile="browser-web",
                )
                self.assertFalse(result.passed)

    def test_rejects_stage1_or_kernel_failure(self) -> None:
        passing = self.passing_web_transcript()
        for marker in (
            b"DEBIAN_ROOTFS_FAIL reason=root-mount\n",
            b"Kernel panic - not syncing\n",
        ):
            with self.subTest(marker=marker):
                result = classify_debug_console_qemu(
                    passing + marker,
                    expected_debian_release="13.6",
                    expected_profile="browser-web",
                )
                self.assertFalse(result.passed)

    def test_rejects_transcript_for_the_other_validated_profile(self) -> None:
        cases = (
            (self.passing_web_transcript(), "desktop-m5-network"),
            (self.passing_m5_transcript(), "browser-web"),
        )
        for transcript, profile in cases:
            with self.subTest(profile=profile):
                result = classify_debug_console_qemu(
                    transcript,
                    expected_debian_release="13.6",
                    expected_profile=profile,
                )
                self.assertFalse(result.passed)

    def test_orchestrator_uses_root_console_classifier(self) -> None:
        config = mock.sentinel.config
        operations = mock.Mock()
        with mock.patch(
            "tools.riscv.debian.rootfs.debug_console_qemu_gate."
            "orchestrate_systemd_m2_gate",
            return_value={"passed": True},
        ) as orchestrate:
            result = orchestrate_debug_console_qemu_gate(config, operations)

        self.assertEqual(result, {"passed": True})
        orchestrate.assert_called_once_with(
            config,
            operations,
            classifier=operations.classify_transcript,
        )


if __name__ == "__main__":
    unittest.main()
