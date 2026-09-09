#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the bounded Megrez physical graphics host gate."""

from __future__ import annotations

import hashlib
import importlib
import base64
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib


MODULE_PATH = Path(__file__).parents[1] / "megrez_physical_graphics.py"
REPOSITORY_ROOT = Path(__file__).parents[3]
MAKEFILE_PATH = REPOSITORY_ROOT / "Makefile"
README_PATH = Path(__file__).parents[1] / "README.md"


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
    test.assertTrue(MODULE_PATH.is_file(), "Megrez physical graphics gate is missing")
    return importlib.import_module("tools.riscv.megrez_physical_graphics")


class PhysicalMarkerTests(unittest.TestCase):
    NONCES = (
        "0123456789abcdef",
        "fedcba9876543210",
        "0011223344556677",
    )

    @classmethod
    def _cycle_lines(cls, cycle: int) -> list[str]:
        nonce = cls.NONCES[cycle - 1]
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        event_hash = hashlib.sha256(f"events-{cycle}".encode()).hexdigest()
        screenshot_hash = hashlib.sha256(f"screen-{cycle}".encode()).hexdigest()
        return [
            f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} nonce_sha256={nonce_hash}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle={cycle} nonce_sha256={nonce_hash}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle={cycle}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle={cycle} key_downs=16 relative_events=2 absolute_events=0 left_down=1 left_up=1 digest={event_hash}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle={cycle} nonce_sha256={nonce_hash} trusted_key=1 trusted_input=1 trusted_pointer=1 trusted_click=1 click_count=1 color=cyan",
            f"ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle={cycle} sha256={screenshot_hash}",
            f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}",
        ]

    @classmethod
    def _passing(cls) -> str:
        lines = [line for cycle in range(1, 4) for line in cls._cycle_lines(cycle)]
        lines.append("ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=3")
        return "boot noise\n" + "\n".join(lines) + "\nrecovery noise\n"

    def test_accepts_three_exact_nonce_bound_cycles(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "classify_interaction_transcript"),
            "interaction marker classifier is missing",
        )
        cycles = gate.classify_interaction_transcript(self._passing(), self.NONCES)
        self.assertEqual([cycle.cycle for cycle in cycles], [1, 2, 3])
        self.assertEqual([cycle.key_downs for cycle in cycles], [16, 16, 16])

    def test_accepts_three_exact_nonce_hash_bound_cycles(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "classify_interaction_hash_transcript"),
            "interaction hash classifier is missing",
        )
        nonce_hashes = tuple(
            hashlib.sha256(nonce.encode()).hexdigest() for nonce in self.NONCES
        )

        cycles = gate.classify_interaction_hash_transcript(
            self._passing(), nonce_hashes
        )

        self.assertEqual(tuple(cycle.nonce_sha256 for cycle in cycles), nonce_hashes)
        with self.assertRaises(gate.HostGateError):
            gate.classify_interaction_hash_transcript(
                self._passing(), (nonce_hashes[0],) * 3
            )

    def test_rejects_missing_duplicate_reordered_and_wrong_nonce_markers(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "classify_interaction_transcript"),
            "interaction marker classifier is missing",
        )
        lines = self._passing().splitlines()
        ready = self._cycle_lines(1)[0]
        wrong_nonce = ready.rsplit("=", 1)[0] + "=" + "0" * 64
        variants = (
            "\n".join(line for line in lines if line != self._cycle_lines(2)[2]),
            "\n".join(lines[:3] + [ready] + lines[3:]),
            "\n".join(lines[:2] + [lines[3], lines[2]] + lines[4:]),
            "\n".join(wrong_nonce if line == ready else line for line in lines),
        )
        for transcript in variants:
            with (
                self.subTest(transcript=transcript[:80]),
                self.assertRaises(gate.HostGateError),
            ):
                gate.classify_interaction_transcript(transcript, self.NONCES)

    def test_rejects_weak_input_and_late_fatal_marker(self) -> None:
        gate = load_gate(self)
        self.assertTrue(
            hasattr(gate, "classify_interaction_transcript"),
            "interaction marker classifier is missing",
        )
        weak = self._passing().replace("key_downs=16", "key_downs=15", 1)
        fatal = self._passing() + "Kernel panic - not syncing\n"
        xhci = self._passing() + "USB HID transfer stopped: timeout\n"
        framebuffer = (
            self._passing()
            + "Firmware framebuffer is not synchronizable; leaving it unregistered\n"
        )
        for transcript in (weak, fatal, xhci, framebuffer):
            with self.assertRaises(gate.HostGateError):
                gate.classify_interaction_transcript(transcript, self.NONCES)

    def test_physical_mode_rejects_absolute_only_tablet_motion(self) -> None:
        gate = load_gate(self)
        tablet_only = self._passing().replace(
            "relative_events=2 absolute_events=0",
            "relative_events=0 absolute_events=2",
        )
        with self.assertRaisesRegex(gate.HostGateError, "physical input"):
            gate.classify_interaction_transcript(tablet_only, self.NONCES)

        cycles = gate.classify_interaction_transcript(
            tablet_only,
            self.NONCES,
            pointer_mode=gate.PointerEvidenceMode.QEMU_TABLET,
        )
        self.assertTrue(all(cycle.absolute_events == 2 for cycle in cycles))

    def test_rejects_reused_screenshot_digest_across_distinct_cycles(self) -> None:
        gate = load_gate(self)
        first = self._cycle_lines(1)[5].rsplit("=", 1)[1]
        stale = self._passing()
        for cycle in (2, 3):
            current = self._cycle_lines(cycle)[5].rsplit("=", 1)[1]
            stale = stale.replace(current, first)
        with self.assertRaisesRegex(gate.HostGateError, "screenshot digest"):
            gate.classify_interaction_transcript(stale, self.NONCES)


class HdmiEvidenceTests(unittest.TestCase):
    def test_ingests_png_and_jpeg_with_digest_and_private_mode(self) -> None:
        gate = load_gate(self)
        self.assertTrue(hasattr(gate, "ingest_hdmi"), "HDMI ingestion is missing")
        payloads = (
            ("png", png_payload(value=0x51)),
            ("jpg", b"\xff\xd8physical-display\xff\xd9"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix, payload in payloads:
                with self.subTest(suffix=suffix):
                    source = root / f"source.{suffix}"
                    destination = root / f"retained.{suffix}"
                    source.write_bytes(payload)
                    evidence = gate.ingest_hdmi(source, destination)
                    self.assertEqual(destination.read_bytes(), payload)
                    self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(
                        evidence.sha256, hashlib.sha256(payload).hexdigest()
                    )
                    self.assertEqual(evidence.size, len(payload))

    def test_rejects_symlink_empty_oversized_and_output_alias(self) -> None:
        gate = load_gate(self)
        self.assertTrue(hasattr(gate, "ingest_hdmi"), "HDMI ingestion is missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.png"
            valid.write_bytes(png_payload(value=0x51))
            symlink = root / "symlink.png"
            symlink.symlink_to(valid)
            empty = root / "empty.png"
            empty.touch()
            incomplete = root / "incomplete.png"
            incomplete.write_bytes(b"\x89PNG\r\n\x1a\npartial")
            oversized = root / "oversized.png"
            with oversized.open("wb") as stream:
                stream.truncate(gate.MAX_HDMI_BYTES + 1)
            for source, destination in (
                (symlink, root / "from-symlink.png"),
                (empty, root / "from-empty.png"),
                (incomplete, root / "from-incomplete.png"),
                (oversized, root / "from-oversized.png"),
                (valid, valid),
            ):
                with (
                    self.subTest(source=source.name),
                    self.assertRaises(gate.HostGateError),
                ):
                    gate.ingest_hdmi(source, destination)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_rejects_fifo_sources_without_blocking(self) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fifo = root / "capture.fifo"
            os.mkfifo(fifo)
            before = time.monotonic()
            with self.assertRaises(gate.HostGateError):
                gate.ingest_hdmi(fifo, root / "retained.png")
            self.assertLess(time.monotonic() - before, 1.0)

    def test_real_attempt_requires_a_capture_created_or_changed_after_start(
        self,
    ) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = (
                repository
                / "target"
                / "current-main-physical-graphics"
                / "physical"
                / "evidence"
            )
            capture = repository / "capture.png"
            stale_payload = png_payload(value=0x51)
            current_payload = png_payload(value=0x52)
            capture.write_bytes(stale_payload)
            operations = gate.RealPhysicalGraphicsOperations(
                SimpleNamespace(artifacts=(), plan_sha256="a" * 64),
                "/dev/null",
                output,
                capture,
                repository=repository,
            )
            try:
                operations.invalidate()
                operations._guest_deadline = time.monotonic() + 900
                with self.assertRaises(TimeoutError):
                    operations.retain_hdmi(0.001)
                update = threading.Timer(
                    0.02,
                    capture.write_bytes,
                    args=(current_payload,),
                )
                update.start()
                try:
                    evidence = operations.retain_hdmi(1.0)
                finally:
                    update.join()
                self.assertEqual(evidence.path.name, "hdmi-evidence.png")
                self.assertEqual(
                    evidence.sha256, hashlib.sha256(capture.read_bytes()).hexdigest()
                )
            finally:
                operations.close()

    def test_real_attempt_waits_for_a_phased_capture_to_become_stable(self) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = (
                repository
                / "target"
                / "current-main-physical-graphics"
                / "physical"
                / "evidence"
            )
            capture = repository / "capture.png"
            payload = png_payload(value=0x53)
            operations = gate.RealPhysicalGraphicsOperations(
                SimpleNamespace(artifacts=(), plan_sha256="a" * 64),
                "/dev/null",
                output,
                capture,
                repository=repository,
            )

            def produce() -> None:
                time.sleep(0.02)
                with capture.open("wb") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                    time.sleep(0.05)
                    stream.write(payload[:32])
                    stream.flush()
                    os.fsync(stream.fileno())
                    time.sleep(0.05)
                    stream.write(payload[32:])
                    stream.flush()
                    os.fsync(stream.fileno())

            operations.invalidate()
            operations._guest_deadline = time.monotonic() + 900
            producer = threading.Thread(target=produce)
            producer.start()
            try:
                evidence = operations.retain_hdmi(2.0)
            finally:
                producer.join()
                operations.close()
            self.assertEqual(evidence.sha256, hashlib.sha256(payload).hexdigest())


class PhysicalCliTests(unittest.TestCase):
    BASE_ARGUMENTS = (
        "/dev/serial/by-id/test",
        "--plan",
        "/tmp/plan.json",
        "--output-directory",
        "/tmp/evidence",
        "--hdmi-capture",
        "/tmp/capture.png",
    )

    def test_cli_accepts_one_complete_mmc_artifact_mapping(self) -> None:
        gate = load_gate(self)
        values = gate.parse_args(
            self.BASE_ARGUMENTS
            + (
                "--mmc-kernel",
                "asterinas-a1b2c3d4-34bc1cc0.Image",
                "--mmc-initramfs",
                "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
                "--mmc-dtb",
                "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb",
            )
        )
        self.assertEqual(values.mmc_kernel, "asterinas-a1b2c3d4-34bc1cc0.Image")
        self.assertEqual(
            values.mmc_initramfs,
            "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
        )
        self.assertEqual(
            values.mmc_dtb,
            "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb",
        )

    def test_cli_rejects_partial_or_unsafe_mmc_artifact_mapping(self) -> None:
        gate = load_gate(self)
        variants = (
            ("--mmc-kernel", "kernel.Image"),
            (
                "--mmc-kernel",
                "kernel.Image",
                "--mmc-initramfs",
                "stage1.cpio",
            ),
            (
                "--mmc-kernel",
                "kernel;reset",
                "--mmc-initramfs",
                "stage1.cpio",
                "--mmc-dtb",
                "board.dtb",
            ),
        )
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(SystemExit):
                gate.parse_args(self.BASE_ARGUMENTS + variant)


class PhysicalMmcArtifactTests(unittest.TestCase):
    def test_real_operations_load_complete_mmc_mapping_without_board_transport(
        self,
    ) -> None:
        gate = load_gate(self)
        artifacts = tuple(
            SimpleNamespace(
                name=name,
                load_address=address,
                size=size,
                crc32=crc32,
            )
            for name, address, size, crc32 in (
                ("kernel", 0x80200000, 101, "11111111"),
                ("initramfs", 0x83000000, 202, "22222222"),
                ("megrez_dtb", 0xF0000000, 303, "33333333"),
            )
        )
        names = {
            "kernel": "asterinas-a1b2c3d4-34bc1cc0.Image",
            "initramfs": "asterinas-a1b2c3d4-34bc1cc0-stage1.cpio",
            "megrez_dtb": (
                "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb"
            ),
        }
        session = mock.Mock()
        session.load_artifact.side_effect = (101, 202, 303)
        operations = gate.RealPhysicalGraphicsOperations(
            SimpleNamespace(artifacts=artifacts),
            "/dev/null",
            Path("/unused"),
            Path("/unused-capture"),
            mmc_artifacts=names,
        )
        operations._session = session
        operations._fd = 41

        with mock.patch.object(
            gate,
            "BoardTransport",
            side_effect=AssertionError("YMODEM path must not be constructed"),
        ):
            outcomes = operations.ensure_artifacts(
                SimpleNamespace(artifacts=artifacts), 300
            )

        self.assertEqual(outcomes, ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc"))
        self.assertEqual(
            session.load_artifact.call_args_list,
            [
                mock.call("kernel", names["kernel"], 0x80200000, "11111111"),
                mock.call("initramfs", names["initramfs"], 0x83000000, "22222222"),
                mock.call("megrez_dtb", names["megrez_dtb"], 0xF0000000, "33333333"),
            ],
        )

    def test_real_operations_reject_mmc_size_mismatch(self) -> None:
        gate = load_gate(self)
        artifacts = (
            SimpleNamespace(
                name="kernel",
                load_address=0x80200000,
                size=101,
                crc32="11111111",
            ),
        )
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._session = mock.Mock()
        operations._session.load_artifact.return_value = 100
        operations._fd = 41
        operations._mmc_artifacts = {"kernel": "kernel.Image"}
        with self.assertRaisesRegex(gate.HostGateError, "size mismatch"):
            operations.ensure_artifacts(SimpleNamespace(artifacts=artifacts), 300)


class PhysicalLifecycleTests(unittest.TestCase):
    NONCES = PhysicalMarkerTests.NONCES
    PLAN_SHA256 = "a" * 64
    PLAN_BOOTARGS = (
        "console=ttyS0 console=tty0 loglevel=info init=/init "
        "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 "
        "asterinas.mmc_write_partition2 asterinas.reboot_after=600 "
        "-- --root-init=systemd"
    )

    class Operations:
        def __init__(
            self,
            gate,
            *,
            recover: bool = True,
            late_fatal: bool = False,
            fail_cycle: int | None = None,
            fail_final: bool = False,
            fail_boot_after_start: bool = False,
            cycle_error: BaseException | None = None,
        ):
            self.gate = gate
            self.recover = recover
            self.late_fatal = late_fatal
            self.fail_cycle = fail_cycle
            self.fail_final = fail_final
            self.fail_boot_after_start = fail_boot_after_start
            self.cycle_error = cycle_error
            self._guest_started = False
            self.events: list[str] = []
            self._transcript: list[str] = ["boot noise"]
            self.published = None

        @property
        def transcript(self) -> str:
            return "\n".join(self._transcript) + "\n"

        @property
        def guest_started(self) -> bool:
            return self._guest_started

        def invalidate(self) -> None:
            self.events.append("invalidate")
            self._guest_started = False

        def open(self, _timeout: float) -> None:
            self.events.append("open")

        def ensure_artifacts(self, _plan, _timeout: float) -> tuple[str, ...]:
            self.events.append("ensure-artifacts")
            return ("kernel:cache-hit", "initramfs:cache-hit", "megrez_dtb:cache-hit")

        def boot(self, _plan, bootargs: str, _timeout: float) -> None:
            self.events.append("boot")
            if "--debug-console=isolated-root" not in bootargs:
                raise AssertionError("physical boot omitted the isolated debug console")
            self._guest_started = True
            if self.fail_boot_after_start:
                raise self.gate.HostGateError("post-boot setup failed")

        def prove_graphical_readiness(self, _timeout: float):
            self.events.append("readiness")
            return self.gate.GraphicalReadinessEvidence(
                browser_pid=41,
                input_nodes=2,
                framebuffer=True,
                xorg_fbdev=True,
                openbox=True,
                firefox=True,
                browser_service="active",
                browser_restarts=0,
                xhci_hosts=2,
                usb_keyboard=True,
                usb_mouse=True,
            )

        def run_cycle(self, cycle: int, nonce: str, _timeout: float) -> bytes:
            self.events.append(f"cycle-{cycle}")
            if cycle == self.fail_cycle:
                if self.cycle_error is not None:
                    raise self.cycle_error
                raise self.gate.HostGateError(f"cycle {cycle} injected failure")
            payload = png_payload(value=0x30 + cycle)
            nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
            event_hash = hashlib.sha256(f"events-{cycle}".encode()).hexdigest()
            screenshot_hash = hashlib.sha256(payload).hexdigest()
            self._transcript.extend(
                (
                    f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} nonce_sha256={nonce_hash}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_KEY_READY cycle={cycle} nonce_sha256={nonce_hash}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_POINTER_READY cycle={cycle}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_INPUT cycle={cycle} key_downs=16 relative_events=2 absolute_events=0 left_down=1 left_up=1 digest={event_hash}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_DOM cycle={cycle} nonce_sha256={nonce_hash} trusted_key=1 trusted_input=1 trusted_pointer=1 trusted_click=1 click_count=1 color=cyan",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_SCREENSHOT cycle={cycle} sha256={screenshot_hash}",
                    f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}",
                )
            )
            return payload

        def retain_hdmi(self, _timeout: float):
            self.events.append("hdmi")
            payload = b"\x89PNG\r\n\x1a\nhdmi"
            return self.gate.FileEvidence(
                path=Path("/retained/hdmi-evidence.png"),
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                format="png",
            )

        def prove_final_state(self, nonce, readiness, _timeout: float) -> None:
            self.events.append("final-state")
            if (
                nonce != PhysicalLifecycleTests.NONCES[-1]
                or readiness.browser_pid != 41
            ):
                raise AssertionError("terminal state was not bound to cycle 3")
            if self.fail_final:
                raise self.gate.HostGateError("terminal state changed")

        def emit_complete(self, _timeout: float) -> None:
            self.events.append("complete")
            self._transcript.append("ASTERINAS_PHYSICAL_GRAPHICS_COMPLETE cycles=3")

        def await_recovery(self, _timeout: float) -> None:
            self.events.append("recovery")
            if not self.recover:
                raise TimeoutError("fresh U-Boot prompt not observed")
            self._transcript.append("OpenSBI v1.7\nU-Boot 2026.07\n=> ")
            if self.late_fatal:
                self._transcript.append("Kernel panic - not syncing")

        def publish(self, result, screenshots, hdmi, outcomes) -> None:
            self.events.append(f"publish:{result.passed}")
            self.published = (result, screenshots, hdmi, outcomes)

        def close(self) -> None:
            self.events.append("close")

    @classmethod
    def _plan(cls):
        return SimpleNamespace(
            bootargs=cls.PLAN_BOOTARGS,
            plan_sha256=cls.PLAN_SHA256,
            validate=lambda: None,
        )

    def test_derives_the_fixed_physical_debug_bootargs(self) -> None:
        gate = load_gate(self)
        bootargs = gate.physical_bootargs(self._plan())
        tokens = bootargs.split()
        self.assertEqual(tokens[:3], ["console=tty0", "console=ttyS0", "loglevel=info"])
        self.assertEqual(tokens.count("asterinas.reboot_after=900"), 1)
        self.assertFalse(any(token.startswith("systemd.unit=") for token in tokens))
        self.assertNotIn("systemd.unit=multi-user.target", tokens)
        self.assertNotIn("asterinas.reboot_after=600", tokens)
        self.assertNotIn("asterinas.mmc_write_partition2", tokens)
        self.assertEqual(
            tokens.count("systemd.mask=asterinas-browser-web-evidence.service"), 1
        )
        self.assertEqual(
            tokens.count("systemd.mask=asterinas-desktop-m5-network.service"), 1
        )
        self.assertEqual(
            tokens.count("systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1"), 1
        )
        self.assertEqual(
            tokens[-3:],
            ["--", "--root-init=systemd", "--debug-console=isolated-root"],
        )

    def test_publishes_pass_only_after_three_cycles_hdmi_and_recovery(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertTrue(result.passed)
        self.assertTrue(result.physical)
        self.assertTrue(result.recovered)
        self.assertEqual([cycle.cycle for cycle in result.cycles], [1, 2, 3])
        self.assertLess(
            operations.events.index("recovery"), operations.events.index("publish:True")
        )
        self.assertEqual(operations.events.count("publish:True"), 1)
        self.assertEqual(operations.events[-1], "close")

    def test_recovery_failure_and_late_fatal_marker_cannot_publish_pass(self) -> None:
        gate = load_gate(self)
        for options in ({"recover": False}, {"late_fatal": True}):
            with self.subTest(options=options):
                operations = self.Operations(gate, **options)
                result = gate.run_physical_graphics(
                    self._plan(),
                    gate.PhysicalGraphicsConfig(),
                    operations,
                    nonces=self.NONCES,
                    artifact_validator=lambda _plan: {},
                )
                self.assertFalse(result.passed)
                self.assertNotIn("publish:True", operations.events)
                self.assertEqual(operations.events[-1], "close")

    def test_early_guest_failure_still_waits_for_recovery_before_publication(
        self,
    ) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate, fail_cycle=2)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertLess(
            operations.events.index("recovery"),
            operations.events.index("publish:False"),
        )
        self.assertEqual(len(operations.published[1]), 1)

    def test_terminal_state_change_after_hdmi_fails_then_recovers(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate, fail_final=True)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertLess(
            operations.events.index("recovery"),
            operations.events.index("publish:False"),
        )

    def test_post_boot_setup_failure_still_recovers(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(gate, fail_boot_after_start=True)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertLess(
            operations.events.index("recovery"),
            operations.events.index("publish:False"),
        )

    def test_unexpected_guest_exception_still_recovers_publishes_and_closes(
        self,
    ) -> None:
        gate = load_gate(self)
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid serial byte")
        operations = self.Operations(gate, fail_cycle=2, cycle_error=error)
        result = gate.run_physical_graphics(
            self._plan(),
            gate.PhysicalGraphicsConfig(),
            operations,
            nonces=self.NONCES,
            artifact_validator=lambda _plan: {},
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertIn("unicode-decode-error", result.reason)
        self.assertLess(
            operations.events.index("recovery"),
            operations.events.index("publish:False"),
        )
        self.assertEqual(operations.events[-1], "close")

    def test_keyboard_interrupt_recovers_and_closes_before_reraising(self) -> None:
        gate = load_gate(self)
        operations = self.Operations(
            gate,
            fail_cycle=2,
            cycle_error=KeyboardInterrupt(),
        )
        with self.assertRaises(KeyboardInterrupt):
            gate.run_physical_graphics(
                self._plan(),
                gate.PhysicalGraphicsConfig(),
                operations,
                nonces=self.NONCES,
                artifact_validator=lambda _plan: {},
            )
        self.assertIn("recovery", operations.events)
        self.assertEqual(operations.events[-1], "close")
        self.assertFalse(
            any(event.startswith("publish:") for event in operations.events)
        )


class PhysicalCommandTests(unittest.TestCase):
    def test_three_slow_cycles_share_the_original_board_reboot_budget(self) -> None:
        gate = load_gate(self)
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._browser_pid = 42
        operations._guest_deadline = 970.0
        serial = mock.Mock(transcript=b"")
        serial.checkpoint.return_value = 0
        operations._serial = serial
        now = [100.0]
        nonce = "0123456789abcdef"
        nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
        lines = iter(
            (elapsed, line)
            for cycle in (1, 2, 3)
            for elapsed, line in (
                (
                    250.0,
                    f"ASTERINAS_PHYSICAL_GRAPHICS_READY cycle={cycle} nonce_sha256={nonce_hash}",
                ),
                (100.0, f"ASTERINAS_PHYSICAL_GRAPHICS_PASS cycle={cycle}"),
                (0.0, f"__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle={cycle} status=0"),
            )
        )

        def next_line(_serial, cursor, deadline):
            elapsed, line = next(lines)
            if now[0] + elapsed >= deadline:
                now[0] = deadline
                raise TimeoutError("bounded serial phase expired")
            now[0] += elapsed
            return line, cursor + 1

        with (
            mock.patch.object(gate.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(operations, "_next_line", side_effect=next_line),
            mock.patch.object(operations, "_sync_serial_log"),
            mock.patch.object(gate, "extract_screenshot_frame", return_value=b"png"),
            mock.patch("builtins.print"),
        ):
            operations.run_cycle(1, nonce, 100.0)
            operations.run_cycle(2, nonce, 100.0)
            with self.assertRaises(TimeoutError):
                operations.run_cycle(3, nonce, 100.0)
        self.assertEqual(now[0], 970.0)
        self.assertTrue(
            all(call.args[1] <= 970.0 for call in serial.send.call_args_list)
        )

    def test_rejects_command_exit_before_pass_without_waiting(self) -> None:
        from tools.riscv import megrez_physical_graphics as gate

        ready = (
            "ASTERINAS_PHYSICAL_GRAPHICS_READY cycle=1 nonce_sha256="
            + hashlib.sha256("0123456789abcdef".encode()).hexdigest()
        )
        for prefix in ([], [ready]):
            for status in ("cycle=1 status=1", "cycle=1 status=0", "malformed"):
                with self.subTest(ready=bool(prefix), status=status):
                    serial = mock.Mock()
                    serial.checkpoint.return_value = 0
                    operations = object.__new__(gate.RealPhysicalGraphicsOperations)
                    operations._serial = serial
                    operations._browser_pid = 42
                    operations._guest_deadline = time.monotonic() + 900
                    lines = iter(
                        [*prefix, "__ASTERINAS_PHYSICAL_COMMAND_STATUS__" + status]
                    )

                    def next_line(_serial, cursor, _deadline):
                        try:
                            return next(lines), cursor + 1
                        except StopIteration:
                            self.fail(
                                "waited for more serial output after command exit"
                            )

                    with (
                        mock.patch.object(
                            operations, "_next_line", side_effect=next_line
                        ),
                        mock.patch("builtins.print"),
                        self.assertRaisesRegex(
                            gate.HostGateError, "exited before PASS"
                        ),
                    ):
                        operations.run_cycle(1, "0123456789abcdef", 30)

    def test_cli_exposes_every_physical_phase_deadline(self) -> None:
        gate = load_gate(self)
        values = gate.parse_args(
            (
                "/dev/serial/by-id/test",
                "--plan",
                "/tmp/plan.json",
                "--output-directory",
                "/tmp/evidence",
                "--hdmi-capture",
                "/tmp/capture.png",
                "--open-timeout",
                "60",
                "--artifact-timeout",
                "300",
                "--boot-timeout",
                "120",
                "--cycle-timeout",
                "180",
                "--hdmi-timeout",
                "60",
                "--recovery-timeout",
                "930",
            )
        )
        self.assertEqual(
            (
                values.open_timeout,
                values.artifact_timeout,
                values.boot_timeout,
                values.cycle_timeout,
                values.hdmi_timeout,
                values.recovery_timeout,
            ),
            (60.0, 300.0, 120.0, 180.0, 60.0, 930.0),
        )

    def test_preflight_binds_fbdev_openbox_firefox_service_and_input_nodes(
        self,
    ) -> None:
        gate = load_gate(self)
        command = gate.physical_preflight_command()
        for fragment in (
            "/dev/fb0",
            "resolution=1920x1080, stride=7680, format=BgrReserved",
            "/dev/input/event*",
            "FBDEV(0)",
            "pgrep -x Xorg",
            "/tmp/.X11-unix/X0",
            "openbox",
            "asterinas-browser-web.service",
            "MainPID",
            "NRestarts",
            "__ASTERINAS_PHYSICAL_PREFLIGHT__",
        ):
            self.assertIn(fragment, command)

    def test_external_service_quiesce_is_fail_closed(self) -> None:
        gate = load_gate(self)
        command = gate.physical_external_services_quiesce_command()
        for fragment in (
            "timeout 60",
            "systemctl mask --runtime",
            "systemctl stop",
            "systemctl start --no-block graphical.target",
            "systemctl reset-failed",
            "asterinas-browser-web-evidence.service",
            "asterinas-desktop-m5-network.service",
            "evidence_state=%s",
            "network_state=%s",
            "__ASTERINAS_PHYSICAL_EXTERNAL__",
        ):
            self.assertIn(fragment, command)
        self.assertLess(
            command.index("systemctl mask --runtime"),
            command.index("systemctl stop"),
        )
        self.assertLess(
            command.index("systemctl stop"),
            command.index("systemctl start --no-block graphical.target"),
        )

    def test_real_gate_requires_quiesced_services_before_preflight(self) -> None:
        gate = load_gate(self)

        class Serial:
            def checkpoint(self) -> int:
                return 7

            def send(self, payload: bytes, deadline: float) -> None:
                del deadline
                self.command = payload.decode()

        serial = Serial()
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._serial = serial
        with mock.patch.object(
            operations,
            "_next_line",
            return_value=(
                "__ASTERINAS_PHYSICAL_EXTERNAL__ status=0 "
                "evidence_state=inactive evidence_pid=0 "
                "network_state=inactive network_pid=0",
                8,
            ),
        ):
            operations._quiesce_external_services(100.0)

        self.assertIn("systemctl stop", serial.command)

    def test_isolated_readiness_starts_graphics_only_after_runtime_masks(self) -> None:
        gate = load_gate(self)
        events: list[str] = []

        class Serial:
            transcript = (gate.DEBUG_CONSOLE_READY + "\n").encode()

            def wait_for(self, marker: bytes, _deadline: float) -> None:
                self_test.assertEqual(marker, gate.DEBUG_CONSOLE_READY.encode())
                events.append("ready")

        self_test = self
        operations = object.__new__(gate.RealPhysicalGraphicsOperations)
        operations._serial = Serial()
        operations._guest_deadline = time.monotonic() + 60
        operations._browser_pid = None
        readiness = gate.GraphicalReadinessEvidence(
            browser_pid=41,
            input_nodes=2,
            framebuffer=True,
            xorg_fbdev=True,
            openbox=True,
            firefox=True,
            browser_service="active",
            browser_restarts=0,
            xhci_hosts=2,
            usb_keyboard=True,
            usb_mouse=True,
        )

        with (
            mock.patch.object(
                gate,
                "validate_debug_console_readiness",
                side_effect=lambda _transcript: events.append("validate"),
            ),
            mock.patch.object(
                gate,
                "run_debug_console_phase",
                side_effect=lambda *_args, **_kwargs: events.append("debug"),
            ),
            mock.patch.object(
                operations,
                "_quiesce_external_services",
                side_effect=lambda _deadline: events.append("quiesce"),
            ),
            mock.patch.object(
                operations,
                "_probe_graphical_readiness",
                side_effect=lambda _deadline: (events.append("probe"), readiness)[1],
            ),
            mock.patch.object(
                operations,
                "_sync_serial_log",
                side_effect=lambda: events.append("sync"),
            ),
        ):
            result = operations.prove_graphical_readiness(30)

        self.assertEqual(result, readiness)
        self.assertEqual(
            events, ["ready", "validate", "quiesce", "probe", "debug", "sync"]
        )

    def test_readiness_requires_two_xhci_hosts_and_both_usb_hid_devices(self) -> None:
        gate = load_gate(self)
        values = {
            "browser_pid": 41,
            "input_nodes": 2,
            "framebuffer": True,
            "xorg_fbdev": True,
            "openbox": True,
            "firefox": True,
            "browser_service": "active",
            "browser_restarts": 0,
            "xhci_hosts": 2,
            "usb_keyboard": True,
            "usb_mouse": True,
        }
        gate.GraphicalReadinessEvidence(**values)
        for mutation in (
            {"xhci_hosts": 1},
            {"usb_keyboard": False},
            {"usb_mouse": False},
            {"framebuffer": False},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(gate.HostGateError):
                gate.GraphicalReadinessEvidence(**(values | mutation))

    def test_cycle_command_joins_firefox_namespace_without_synthesizing_input(
        self,
    ) -> None:
        gate = load_gate(self)
        command = gate.physical_cycle_command(2, "0123456789abcdef", 180.0)
        for fragment in (
            "nsenter",
            "asterinas-browser-web.service",
            "/usr/lib/asterinas/physical-graphics-gate",
            "--nonce 0123456789abcdef",
            "--cycle 2",
            "--setup-timeout 300",
            "__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle=2 status=%s",
        ):
            self.assertIn(fragment, command)
        for forbidden in (
            "xdotool",
            "PerformActions",
            "ElementClick",
            "key ",
            "mouse_move",
        ):
            self.assertNotIn(forbidden, command)
        for cycle, nonce, timeout in (
            (0, "0123456789abcdef", 180.0),
            (1, "not-a-nonce", 180.0),
            (1, "0123456789abcdef", 301.0),
        ):
            with (
                self.subTest(cycle=cycle, nonce=nonce, timeout=timeout),
                self.assertRaises(ValueError),
            ):
                gate.physical_cycle_command(cycle, nonce, timeout)
        with self.assertRaises(ValueError):
            gate.physical_cycle_command(
                1, "0123456789abcdef", 180.0, setup_timeout=901.0
            )

    def test_final_command_rechecks_cycle_three_without_navigation_or_input(
        self,
    ) -> None:
        gate = load_gate(self)
        command = gate.physical_final_command("0123456789abcdef", 41, 180.0)
        for fragment in (
            "nsenter",
            "--cycle 3",
            "--firefox-pid",
            "--verify-final",
            "--setup-timeout 300",
            "__ASTERINAS_PHYSICAL_FINAL_STATUS__",
        ):
            self.assertIn(fragment, command)
        for forbidden in ("xdotool", "PerformActions", "ElementClick", "mouse_move"):
            self.assertNotIn(forbidden, command)


class ScreenshotTransferTests(unittest.TestCase):
    def test_extracts_one_bounded_hash_bound_serial_png(self) -> None:
        gate = load_gate(self)
        payload = png_payload(value=0x42)
        digest = hashlib.sha256(payload).hexdigest()
        transcript = (
            "shell echo noise\n"
            f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=2 size={len(payload)} sha256={digest}\n"
            f"{base64.b64encode(payload).decode()}\n"
            "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=2\n"
        )
        self.assertEqual(gate.extract_screenshot_frame(transcript, 2), payload)

    def test_extracts_frame_from_serial_transcript_with_double_carriage_returns(
        self,
    ) -> None:
        gate = load_gate(self)
        payload = png_payload(value=0x43)
        digest = hashlib.sha256(payload).hexdigest()
        transcript = (
            "shell echo noise\r\r\n"
            f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=2 size={len(payload)} sha256={digest}\r\r\n"
            f"{base64.b64encode(payload).decode()}\r\r\n"
            "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=2\r\r\n"
        )
        self.assertEqual(gate.extract_screenshot_frame(transcript, 2), payload)

    def test_rejects_duplicate_mismatched_and_oversized_screenshot_frames(self) -> None:
        gate = load_gate(self)
        payload = png_payload(value=0x42)
        digest = hashlib.sha256(payload).hexdigest()
        frame = (
            f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=1 size={len(payload)} sha256={digest}\n"
            f"{base64.b64encode(payload).decode()}\n"
            "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=1\n"
        )
        variants = (
            frame + frame,
            frame.replace(digest, "0" * 64),
            frame.replace(
                f"size={len(payload)}", f"size={gate.MAX_GUEST_SCREENSHOT_BYTES + 1}"
            ),
        )
        for transcript in variants:
            with self.assertRaises(gate.HostGateError):
                gate.extract_screenshot_frame(transcript, 1)

    def test_rejects_structurally_invalid_or_wrong_size_png_frame(self) -> None:
        gate = load_gate(self)
        for payload in (png_payload(width=1280, height=1024), b"\x89PNG\r\n\x1a\nbad"):
            digest = hashlib.sha256(payload).hexdigest()
            frame = (
                f"__ASTERINAS_PHYSICAL_SCREENSHOT_BEGIN__ cycle=1 "
                f"size={len(payload)} sha256={digest}\n"
                f"{base64.b64encode(payload).decode()}\n"
                "__ASTERINAS_PHYSICAL_SCREENSHOT_END__ cycle=1\n"
            )
            with self.subTest(size=len(payload)), self.assertRaises(gate.HostGateError):
                gate.extract_screenshot_frame(frame, 1)


class PublicationTests(unittest.TestCase):
    def test_result_is_the_last_commit_marker_and_is_listed_in_hashes(self) -> None:
        gate = load_gate(self)

        class Output:
            path = Path("/retained")

            def __init__(self) -> None:
                self.payloads: dict[str, bytes] = {}
                self.writes: list[str] = []

            def atomic_write(self, name: str, payload: bytes, *, mode: int) -> None:
                self.assert_private(mode)
                self.payloads[name] = payload
                self.writes.append(name)

            @staticmethod
            def assert_private(mode: int) -> None:
                if mode != 0o600:
                    raise AssertionError("published evidence is not private")

            def sha256(self, name: str) -> str:
                return hashlib.sha256(self.payloads[name]).hexdigest()

        plan = SimpleNamespace(artifacts=(), plan_sha256="a" * 64)
        operations = gate.RealPhysicalGraphicsOperations(
            plan,
            "/dev/null",
            Path("/unused"),
            Path("/unused-capture"),
        )
        output = Output()
        operations._output = output
        result = gate.PhysicalGraphicsResult(
            schema_version=1,
            passed=False,
            physical=True,
            reason="diagnostic",
            plan_sha256="a" * 64,
            bootargs_sha256="b" * 64,
            recovered=False,
            readiness=None,
            cycles=(),
            hdmi=None,
            transport=(),
        )
        operations.publish(result, (), None, ())
        self.assertEqual(output.writes[-2:], ["sha256sums.txt", "result.json"])
        expected = hashlib.sha256(result.canonical_bytes()).hexdigest()
        self.assertIn(
            f"{expected}  result.json\n",
            output.payloads["sha256sums.txt"].decode(),
        )

    def test_physical_result_cannot_be_mislabeled_as_simulated(self) -> None:
        gate = load_gate(self)
        with self.assertRaisesRegex(gate.HostGateError, "simulated"):
            gate.PhysicalGraphicsResult(
                schema_version=1,
                passed=False,
                physical=False,
                reason="diagnostic",
                plan_sha256="a" * 64,
                bootargs_sha256="b" * 64,
                recovered=False,
                readiness=None,
                cycles=(),
                hdmi=None,
                transport=(),
            )


class PlanInputTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_rejects_fifo_plan_without_blocking(self) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "plan.fifo"
            os.mkfifo(fifo)
            before = time.monotonic()
            with self.assertRaises(gate.HostGateError):
                gate._read_plan(fifo)
            self.assertLess(time.monotonic() - before, 1.0)


class OutputDirectoryTests(unittest.TestCase):
    def test_accepts_only_the_current_main_physical_evidence_tree(self) -> None:
        gate = load_gate(self)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            expected = (
                repository
                / "target"
                / "current-main-physical-graphics"
                / "physical"
                / "evidence"
            )
            self.assertEqual(
                gate._safe_output_directory(expected, repository), expected
            )
            for rejected in (
                repository / "target" / "megrez-physical-graphics" / "attempt",
                repository / "target" / "unrelated",
            ):
                with self.assertRaises(gate.HostGateError):
                    gate._safe_output_directory(rejected, repository)


class DocumentationTests(unittest.TestCase):
    def test_makefile_exposes_unit_qemu_and_non_mutating_prepare_targets(self) -> None:
        makefile = MAKEFILE_PATH.read_text()
        for target in (
            "test_riscv_physical_graphics_unit",
            "test_riscv_physical_graphics_qemu_gate",
            "prepare_riscv_megrez_physical_graphics",
        ):
            self.assertIn(f".PHONY: {target}", makefile)
            self.assertIn(f"{target}:", makefile)

        unit_recipe = makefile.split("test_riscv_physical_graphics_unit:", 1)[1].split(
            ".PHONY:", 1
        )[0]
        for module in (
            "tools.riscv.tests.test_physical_graphics_gate",
            "tools.riscv.tests.test_megrez_physical_graphics",
            "tools.riscv.tests.test_physical_graphics_qemu_gate",
        ):
            self.assertIn(module, unit_recipe)

        qemu_recipe = makefile.split("test_riscv_physical_graphics_qemu_gate:", 1)[
            1
        ].split(".PHONY:", 1)[0]
        for variable in (
            "DEBIAN_KERNEL",
            "DEBIAN_UBOOT",
            "DEBIAN_DTB",
            "DEBIAN_STAGE1_INITRAMFS",
            "DEBIAN_ROOT_IMAGE",
            "DEBIAN_ROOT_MANIFEST",
            "DEBIAN_PACKAGES_LOCK",
            "DEBIAN_PACKAGE_CHECKSUMS",
            "RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT",
        ):
            self.assertIn(f"$({variable})", qemu_recipe)
        self.assertIn("tools.riscv.physical_graphics_qemu_gate", qemu_recipe)
        self.assertIn("--command-timeout 300", qemu_recipe)

        prepare_recipe = makefile.split("prepare_riscv_megrez_physical_graphics:", 1)[
            1
        ].split(".PHONY:", 1)[0]
        self.assertIn(
            "MEGREZ_PHYSICAL_GRAPHICS_OUTPUT ?= "
            "$(CURDIR)/target/current-main-physical-graphics/physical/evidence",
            makefile,
        )
        self.assertIn("_validate_current_artifacts", prepare_recipe)
        self.assertIn("_read_plan", prepare_recipe)
        self.assertIn("--hdmi-capture", prepare_recipe)
        for option in (
            "--open-timeout 60",
            "--artifact-timeout 300",
            "--boot-timeout 120",
            "--cycle-timeout 180",
            "--hdmi-timeout 60",
            "--recovery-timeout 930",
        ):
            self.assertIn(option, prepare_recipe)
        self.assertIn(
            '--output-directory "$(MEGREZ_PHYSICAL_GRAPHICS_OUTPUT)"',
            prepare_recipe,
        )
        self.assertNotIn("tools.riscv.megrez_debug board", prepare_recipe)
        self.assertNotIn("run_physical_graphics", prepare_recipe)

    def test_operator_guide_freezes_source_inputs_and_physical_boundaries(self) -> None:
        readme = README_PATH.read_text()
        section = readme.split("## Current-main Megrez physical graphics", 1)[1]
        prose = " ".join(section.split())
        self.assertIn("69a7b6e41ca74932f79d917f3638199da573b1e9", section)
        for option in (
            "--kernel",
            "--uboot",
            "--dtb",
            "--stage1-initramfs",
            "--root-image",
            "--root-manifest",
            "--packages-lock",
            "--package-checksums",
            "--hdmi-capture",
        ):
            self.assertIn(option, section)
        self.assertIn("seven immutable supporting inputs", prose)
        self.assertIn("three 180-second", prose)
        self.assertIn("asterinas.reboot_after=900", section)
        self.assertIn("QEMU cannot satisfy the physical result", prose)
        self.assertIn("set -euo pipefail", section)
        self.assertIn("source-identity.txt", section)
        self.assertIn("prepare_riscv_megrez_physical_graphics", section)
        self.assertIn("test_riscv_physical_graphics_qemu_gate", section)
        self.assertIn("physical/operator-hdmi.png", section)
        self.assertIn("physical/evidence", section)


if __name__ == "__main__":
    unittest.main()
