#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the bounded Megrez kernel-probe loop."""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import zlib
from unittest import mock

from tools.riscv import megrez_probe as probe
from tools.riscv.megrez_debug_contract import ArtifactIdentity, DebugPlan


SERIAL_DEVICE = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_TEST-if00-port0"
MMC_PATHS = {
    "kernel": "asterinas-test.Image",
    "initramfs": "asterinas-test-stage1.cpio",
    "megrez_dtb": "dtbs/linux/eswin/eic7700-milkv-megrez.dtb",
}


def _plan() -> DebugPlan:
    addresses = {
        "kernel": 0x80200000,
        "initramfs": 0x83000000,
        "qemu_dtb": 0xF0000000,
        "megrez_dtb": 0xF0000000,
    }
    artifacts = tuple(
        ArtifactIdentity(
            name=name,
            path=str((Path("/tmp") / name).absolute()),
            load_address=addresses[name],
            size=100 + index,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
            crc32=f"{zlib.crc32(name.encode()):08x}",
        )
        for index, name in enumerate(("kernel", "initramfs", "qemu_dtb", "megrez_dtb"))
    )
    return DebugPlan(
        schema_version=1,
        profile="tcp-probe",
        artifacts=artifacts,
        bootargs="loglevel=info init=/init asterinas.reboot_after=180",
        smp=4,
        sv39=True,
        markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
        reboot_after=180,
    )


def _bundle_mapping() -> dict[str, object]:
    plan = _plan()
    return {
        "schema_version": 1,
        "plan": plan.to_dict(),
        "plan_sha256": plan.plan_sha256,
        "device": SERIAL_DEVICE,
        "mmc_artifacts": [
            {"name": name, "path": MMC_PATHS[name]}
            for name in ("kernel", "initramfs", "megrez_dtb")
        ],
    }


def _encoded(mapping: dict[str, object]) -> bytes:
    return (json.dumps(mapping, sort_keys=True, separators=(",", ":")) + "\n").encode()


class ProbeBundleTests(unittest.TestCase):
    def test_canonical_bundle_round_trip_is_hash_bound(self) -> None:
        bundle = probe.ProbeBundle.from_bytes(_encoded(_bundle_mapping()))

        self.assertEqual(bundle.device, SERIAL_DEVICE)
        self.assertEqual(bundle.plan.plan_sha256, bundle.plan_sha256)
        self.assertEqual(
            tuple(item.name for item in bundle.mmc_artifacts),
            ("kernel", "initramfs", "megrez_dtb"),
        )
        self.assertEqual(
            bundle.bundle_sha256,
            hashlib.sha256(bundle.canonical_bytes()).hexdigest(),
        )
        self.assertEqual(probe.ProbeBundle.from_bytes(bundle.canonical_bytes()), bundle)

    def test_bundle_rejects_unknown_fields(self) -> None:
        mapping = _bundle_mapping()
        mapping["extra"] = "unsafe"

        with self.assertRaisesRegex(probe.ProbeContractError, "fields"):
            probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_non_by_id_serial_device(self) -> None:
        mapping = _bundle_mapping()
        mapping["device"] = "/dev/ttyUSB0"

        with self.assertRaisesRegex(probe.ProbeContractError, "serial device"):
            probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_unsafe_mmc_paths(self) -> None:
        for invalid in ("/boot/kernel", "../kernel", "dir//kernel", "dir/./kernel"):
            with self.subTest(invalid=invalid):
                mapping = _bundle_mapping()
                mapping["mmc_artifacts"][0]["path"] = invalid  # type: ignore[index]

                with self.assertRaisesRegex(probe.ProbeContractError, "MMC path"):
                    probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_mmc_artifact_name_mismatch(self) -> None:
        mapping = _bundle_mapping()
        mapping["mmc_artifacts"][0]["name"] = "initramfs"  # type: ignore[index]

        with self.assertRaisesRegex(probe.ProbeContractError, "canonical order"):
            probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_changed_plan_identity_fields(self) -> None:
        for field, value in (
            ("size", 999),
            ("crc32", "f" * 8),
            ("load_address", 0x80400000),
        ):
            with self.subTest(field=field):
                mapping = _bundle_mapping()
                mapping["plan"]["artifacts"][0][field] = value  # type: ignore[index]

                with self.assertRaises(probe.ProbeContractError):
                    probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_plan_digest_mismatch(self) -> None:
        mapping = _bundle_mapping()
        mapping["plan_sha256"] = "f" * 64

        with self.assertRaisesRegex(probe.ProbeContractError, "plan digest"):
            probe.ProbeBundle.from_bytes(_encoded(mapping))


class ProbeSelectionTests(unittest.TestCase):
    def test_probe_names_preserve_requested_order(self) -> None:
        selected = probe.validate_probe_names(("syscall272", "boot", "syscall213"))

        self.assertEqual(
            tuple(definition.name for definition in selected),
            ("syscall272", "boot", "syscall213"),
        )

    def test_all_fixed_probe_names_are_accepted(self) -> None:
        names = (
            "boot",
            "syscall213",
            "syscall272",
            "ext2-writeback",
            "systemd-compat",
        )

        self.assertEqual(
            tuple(item.name for item in probe.validate_probe_names(names)), names
        )

    def test_invalid_probe_selections_are_rejected(self) -> None:
        for names in (
            (),
            ("boot", "boot"),
            ("unknown",),
            (" boot",),
            (
                "boot",
                "syscall213",
                "syscall272",
                "ext2-writeback",
                "systemd-compat",
                "x",
            ),
        ):
            with self.subTest(names=names), self.assertRaises(probe.ProbeContractError):
                probe.validate_probe_names(names)

    def test_session_seconds_are_bounded_canonical_integers(self) -> None:
        self.assertEqual(probe.validate_session_seconds(None), 90)
        self.assertEqual(probe.validate_session_seconds(30), 30)
        self.assertEqual(probe.validate_session_seconds(300), 300)
        for invalid in (True, 29, 301, 30.0):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(probe.ProbeContractError),
            ):
                probe.validate_session_seconds(invalid)

    def test_probe_bootargs_remove_every_partition_write_flag_spelling(self) -> None:
        for token in (
            "asterinas.mmc_write_partition2",
            "asterinas.mmc_write_partition2=1",
            "asterinas.mmc-write-partition2=yes",
        ):
            with self.subTest(token=token):
                plan = replace(_plan(), bootargs=f"{_plan().bootargs} {token}")
                bootargs = probe.probe_bootargs(plan, 90)
                normalized_keys = {
                    item.partition("=")[0].replace("-", "_")
                    for item in bootargs.split()
                }
                self.assertNotIn("asterinas.mmc_write_partition2", normalized_keys)


class ProbeProtocolTests(unittest.TestCase):
    NONCE = "00112233445566778899aabbccddeeff"

    def _success(self) -> bytes:
        return (
            "kernel boot noise\n"
            "ASTERINAS_PROBE_READY v=1 pid=1\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=0 name=boot\n"
            f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=0 name=boot detail=boot-ok\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=1 name=syscall213\n"
            f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=1 name=syscall213 detail=enosys\n"
            f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} count=2 status=pass\n"
            f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={self.NONCE}\n"
        ).encode()

    def test_classifies_one_complete_ordered_exchange(self) -> None:
        exchange = probe.classify_probe_transcript(
            self._success(), self.NONCE, ("boot", "syscall213")
        )

        self.assertTrue(exchange.passed)
        self.assertEqual(
            tuple(
                (item.sequence, item.name, item.passed, item.detail)
                for item in exchange.outcomes
            ),
            ((0, "boot", True, "boot-ok"), (1, "syscall213", True, "enosys")),
        )
        self.assertEqual(exchange.dmesg, b"")

    def test_classifies_one_exact_tty_echo_before_the_first_start(self) -> None:
        request = (
            f"ASTERINAS_PROBE_RUN v=1 nonce={self.NONCE} "
            "probes=boot,syscall213 shell=0\n"
        )
        transcript = self._success().replace(
            b"ASTERINAS_PROBE_READY v=1 pid=1\n",
            b"ASTERINAS_PROBE_READY v=1 pid=1\n" + request.encode(),
        )

        exchange = probe.classify_probe_transcript(
            transcript, self.NONCE, ("boot", "syscall213")
        )

        self.assertTrue(exchange.passed)
        for replacement in (
            request.replace("syscall213", "syscall272"),
            request + request,
        ):
            invalid = self._success().replace(
                b"ASTERINAS_PROBE_READY v=1 pid=1\n",
                b"ASTERINAS_PROBE_READY v=1 pid=1\n" + replacement.encode(),
            )
            with self.assertRaises(probe.ProbeProtocolError):
                probe.classify_probe_transcript(
                    invalid, self.NONCE, ("boot", "syscall213")
                )

    def test_classifies_fail_fast_exchange_with_bounded_dmesg(self) -> None:
        transcript = (
            "ASTERINAS_PROBE_READY v=1 pid=1\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=0 name=boot\n"
            f"ASTERINAS_PROBE_FAIL v=1 nonce={self.NONCE} seq=0 name=boot errno=5 detail=uname-failed\n"
            f"ASTERINAS_PROBE_DMESG_BEGIN v=1 nonce={self.NONCE} bytes=12\n"
            "hello dmesg\n"
            f"ASTERINAS_PROBE_DMESG_END v=1 nonce={self.NONCE}\n"
            f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} count=1 status=fail\n"
            f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={self.NONCE}\n"
        ).encode()

        exchange = probe.classify_probe_transcript(
            transcript, self.NONCE, ("boot", "syscall213")
        )

        self.assertFalse(exchange.passed)
        self.assertEqual(exchange.outcomes[0].error_number, 5)
        self.assertEqual(exchange.dmesg, b"hello dmesg\n")

    def test_request_encoding_is_exact_and_shell_is_preselected(self) -> None:
        selected = probe.validate_probe_names(("boot", "syscall213"))

        self.assertEqual(
            probe.encode_probe_request(self.NONCE, selected),
            (
                f"ASTERINAS_PROBE_RUN v=1 nonce={self.NONCE} "
                "probes=boot,syscall213 shell=0\n"
            ).encode(),
        )
        self.assertEqual(
            probe.encode_probe_request(self.NONCE, selected, shell=True),
            (
                f"ASTERINAS_PROBE_RUN v=1 nonce={self.NONCE} "
                "probes=boot,syscall213 shell=1\n"
            ).encode(),
        )

    def test_protocol_rejects_missing_or_replayed_terminal_records(self) -> None:
        valid = self._success().decode()
        variants = (
            valid.replace(
                "kernel boot noise\n",
                "kernel boot noise\n"
                f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} "
                "seq=0 name=boot detail=boot-ok\n",
            ),
            valid.replace(
                f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} count=2 status=pass\n",
                "",
            ),
            valid.replace(
                f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=0 name=boot detail=boot-ok\n",
                f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=0 name=boot detail=boot-ok\n"
                f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=0 name=boot detail=boot-ok\n",
            ),
            valid.replace("seq=1 name=syscall213", "seq=0 name=syscall213"),
        )
        for transcript in variants:
            with (
                self.subTest(transcript=transcript[-200:]),
                self.assertRaises(probe.ProbeProtocolError),
            ):
                probe.classify_probe_transcript(
                    transcript.encode(), self.NONCE, ("boot", "syscall213")
                )

    def test_shell_protocol_accepts_crlf_without_discarding_batch_records(self) -> None:
        valid = (
            self._success()
            .replace(
                f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={self.NONCE}\n".encode(),
                (
                    f"ASTERINAS_PROBE_SHELL_READY v=1 nonce={self.NONCE}\n"
                    "ASTERINAS_PROBE_SHELL_COMMANDS "
                    "help,dmesg,mounts,boot,syscall213,syscall272,"
                    "ext2-writeback,systemd-compat,exit\n"
                    f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={self.NONCE}\n"
                ).encode(),
            )
            .replace(b"\n", b"\r\n")
        )

        exchange = probe.classify_probe_transcript(
            valid, self.NONCE, ("boot", "syscall213"), shell=True
        )

        self.assertTrue(exchange.passed)
        replay = valid.replace(
            b"ASTERINAS_PROBE_SHELL_READY",
            (
                f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} "
                "count=2 status=pass\r\nASTERINAS_PROBE_SHELL_READY"
            ).encode(),
        )
        with self.assertRaises(probe.ProbeProtocolError):
            probe.classify_probe_transcript(
                replay, self.NONCE, ("boot", "syscall213"), shell=True
            )

    def test_protocol_rejects_identity_substitution_and_unknown_markers(self) -> None:
        valid = self._success().decode()
        variants = (
            valid.replace(self.NONCE, "f" * 32, 1),
            valid.replace("seq=1 name=syscall213", "seq=1 name=syscall272", 1),
            valid.replace("detail=enosys", "detail=unsafe/value"),
            valid.replace(
                f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE}",
                f"ASTERINAS_PROBE_UNKNOWN v=1 nonce={self.NONCE}\n"
                f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE}",
            ),
            valid.replace("count=2 status=pass", "count=1 status=pass"),
        )
        for transcript in variants:
            with (
                self.subTest(transcript=transcript[-200:]),
                self.assertRaises(probe.ProbeProtocolError),
            ):
                probe.classify_probe_transcript(
                    transcript.encode(), self.NONCE, ("boot", "syscall213")
                )

    def test_protocol_rejects_pass_after_failure(self) -> None:
        transcript = (
            "ASTERINAS_PROBE_READY v=1 pid=1\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=0 name=boot\n"
            f"ASTERINAS_PROBE_FAIL v=1 nonce={self.NONCE} seq=0 name=boot errno=5 detail=uname-failed\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=1 name=syscall213\n"
            f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=1 name=syscall213 detail=enosys\n"
            f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} count=2 status=fail\n"
            f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={self.NONCE}\n"
        ).encode()

        with self.assertRaises(probe.ProbeProtocolError):
            probe.classify_probe_transcript(
                transcript, self.NONCE, ("boot", "syscall213")
            )


class _LifecycleOperations:
    def __init__(
        self,
        events: list[str],
        exchange: probe.ProbeExchange,
        *,
        fail_exchange: bool = False,
        recover: bool = True,
    ) -> None:
        self.events = events
        self._exchange = exchange
        self._fail_exchange = fail_exchange
        self._recover = recover
        self._guest_started = False
        self._transcript = b"kernel\nASTERINAS_PROBE_READY v=1 pid=1\n"

    @property
    def guest_started(self) -> bool:
        return self._guest_started

    @property
    def transcript(self) -> bytes:
        return self._transcript

    def open(self, timeout: float) -> None:
        self.events.append(f"open:{timeout:.0f}")

    def ensure_artifacts(self, timeout: float) -> tuple[str, ...]:
        self.events.append(f"artifacts:{timeout:.0f}")
        return ("kernel:mmc", "initramfs:mmc", "megrez_dtb:mmc")

    def boot(self, bootargs: str, timeout: float) -> None:
        self.events.append(f"boot:{timeout:.0f}")
        for forbidden in (
            "asterinas.net=",
            "asterinas.neighbor=",
            "asterinas.mmc_write_partition2",
            "systemd.",
            "firefox",
        ):
            if forbidden in bootargs:
                raise AssertionError(f"forbidden fast-probe bootarg: {forbidden}")
        if not bootargs.endswith("asterinas.reboot_after=90 -- --root-init=probe"):
            raise AssertionError(f"unexpected probe bootargs: {bootargs}")
        self._guest_started = True

    def exchange(
        self,
        nonce: str,
        selected: tuple[probe.ProbeDefinition, ...],
        shell: bool,
        timeout: float,
    ) -> probe.ProbeExchange:
        self.events.append(
            f"exchange:{','.join(item.name for item in selected)}:{int(shell)}:{timeout:.0f}"
        )
        if self._fail_exchange:
            raise TimeoutError("injected exchange timeout")
        return self._exchange

    def request_reboot(self, nonce: str, timeout: float) -> None:
        self.events.append(f"request-reboot:{len(nonce)}:{timeout:.0f}")

    def await_recovery(self, timeout: float) -> None:
        self.events.append(f"recovery:{timeout:.0f}")
        if not self._recover:
            raise TimeoutError("fresh U-Boot epoch not observed")

    def close(self) -> None:
        self.events.append("close")


class _LifecyclePublisher:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.published = None

    def invalidate(self) -> None:
        self.events.append("invalidate")

    def publish(self, result, transcript: bytes, dmesg: bytes) -> None:
        self.events.append(f"publish:{result.passed}")
        self.published = (result, transcript, dmesg)


class ProbeLifecycleTests(unittest.TestCase):
    def _bundle(self) -> probe.ProbeBundle:
        return probe.ProbeBundle.from_bytes(_encoded(_bundle_mapping()))

    @staticmethod
    def _clock():
        value = 100.0

        def clock() -> float:
            nonlocal value
            current = value
            value += 1.0
            return current

        return clock

    def test_multiple_probes_use_one_boot_and_publish_after_recovery(self) -> None:
        events: list[str] = []
        exchange = probe.ProbeExchange(
            outcomes=(
                probe.ProbeOutcome(0, "boot", True, None, "boot-ok"),
                probe.ProbeOutcome(1, "syscall213", True, None, "enosys"),
            ),
            passed=True,
            dmesg=b"",
        )
        operations = _LifecycleOperations(events, exchange)
        publisher = _LifecyclePublisher(events)

        result = probe.run_probe(
            self._bundle(),
            probe.validate_probe_names(("boot", "syscall213")),
            probe.ProbeRunConfig(),
            operations,
            publisher,
            clock=self._clock(),
            nonce_factory=lambda: "0" * 32,
        )

        self.assertTrue(result.passed)
        self.assertTrue(result.recovered)
        self.assertEqual(
            [event.split(":", 1)[0] for event in events],
            [
                "invalidate",
                "open",
                "artifacts",
                "boot",
                "exchange",
                "request-reboot",
                "recovery",
                "close",
                "publish",
            ],
        )
        self.assertEqual(result.selected_probes, ("boot", "syscall213"))
        self.assertEqual(len(result.outcomes), 2)

    def test_fatal_transcript_cannot_publish_a_successful_probe(self) -> None:
        events: list[str] = []
        exchange = probe.ProbeExchange(
            (probe.ProbeOutcome(0, "boot", True, None, "boot-ok"),),
            True,
            b"",
        )
        operations = _LifecycleOperations(events, exchange)
        operations._transcript += b"Kernel panic - not syncing\n"
        publisher = _LifecyclePublisher(events)

        result = probe.run_probe(
            self._bundle(),
            probe.validate_probe_names(("boot",)),
            probe.ProbeRunConfig(),
            operations,
            publisher,
            clock=self._clock(),
            nonce_factory=lambda: "0" * 32,
        )

        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertEqual(result.reason, "probe-fatal-diagnostics")

    def test_invalid_bundle_cannot_leave_a_stale_terminal_result(self) -> None:
        events: list[str] = []
        publisher = _LifecyclePublisher(events)
        invalid = replace(self._bundle(), plan_sha256="f" * 64)

        with self.assertRaises(probe.ProbeContractError):
            probe.run_probe(
                invalid,
                probe.validate_probe_names(("boot",)),
                probe.ProbeRunConfig(),
                _LifecycleOperations(events, probe.ProbeExchange((), False, b"")),
                publisher,
            )

        self.assertEqual(events, ["invalidate"])

    def test_exchange_timeout_relies_on_deadline_then_recovers(self) -> None:
        events: list[str] = []
        operations = _LifecycleOperations(
            events,
            probe.ProbeExchange((), False, b""),
            fail_exchange=True,
        )
        publisher = _LifecyclePublisher(events)

        result = probe.run_probe(
            self._bundle(),
            probe.validate_probe_names(("boot",)),
            probe.ProbeRunConfig(),
            operations,
            publisher,
            clock=self._clock(),
            nonce_factory=lambda: "0" * 32,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "probe-exchange-failed")
        self.assertNotIn("request-reboot", [event.split(":", 1)[0] for event in events])
        self.assertIn("recovery:117", events)

    def test_serial_overflow_still_closes_and_publishes_failure(self) -> None:
        events: list[str] = []

        class OverflowOperations(_LifecycleOperations):
            def exchange(self, *_args):
                raise BufferError("serial transcript exceeds byte cap")

            def await_recovery(self, _timeout: float) -> None:
                raise EOFError("serial console closed")

        publisher = _LifecyclePublisher(events)
        result = probe.run_probe(
            self._bundle(),
            probe.validate_probe_names(("boot",)),
            probe.ProbeRunConfig(),
            OverflowOperations(events, probe.ProbeExchange((), False, b"")),
            publisher,
            clock=self._clock(),
            nonce_factory=lambda: "0" * 32,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "manual-reset-required")
        self.assertLess(events.index("close"), events.index("publish:False"))

    def test_failed_immediate_reboot_request_cannot_publish_pass(self) -> None:
        events: list[str] = []

        class RebootFailureOperations(_LifecycleOperations):
            def request_reboot(self, _nonce: str, _timeout: float) -> None:
                raise TimeoutError("reboot request was not accepted")

        exchange = probe.ProbeExchange(
            (probe.ProbeOutcome(0, "boot", True, None, "boot-ok"),),
            True,
            b"",
        )
        publisher = _LifecyclePublisher(events)
        result = probe.run_probe(
            self._bundle(),
            probe.validate_probe_names(("boot",)),
            probe.ProbeRunConfig(),
            RebootFailureOperations(events, exchange),
            publisher,
            clock=self._clock(),
            nonce_factory=lambda: "0" * 32,
        )

        self.assertFalse(result.passed)
        self.assertTrue(result.recovered)
        self.assertEqual(result.reason, "probe-reboot-request-failed")

    def test_missing_recovery_reports_manual_reset_required(self) -> None:
        events: list[str] = []
        operations = _LifecycleOperations(
            events,
            probe.ProbeExchange(
                (probe.ProbeOutcome(0, "boot", True, None, "boot-ok"),),
                True,
                b"",
            ),
            recover=False,
        )
        publisher = _LifecyclePublisher(events)

        result = probe.run_probe(
            self._bundle(),
            probe.validate_probe_names(("boot",)),
            probe.ProbeRunConfig(),
            operations,
            publisher,
            clock=self._clock(),
            nonce_factory=lambda: "0" * 32,
        )

        self.assertFalse(result.passed)
        self.assertFalse(result.recovered)
        self.assertEqual(result.reason, "manual-reset-required")
        self.assertEqual(events[-1], "publish:False")


class ProbePublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name) / "probe-output"
        self.bundle = probe.ProbeBundle.from_bytes(_encoded(_bundle_mapping()))

    def _result(self, *, passed: bool = True) -> probe.ProbeRunResult:
        return probe.ProbeRunResult(
            schema_version=1,
            passed=passed,
            reason="probe-pass" if passed else "probe-failed",
            bundle_sha256=self.bundle.bundle_sha256,
            plan_sha256=self.bundle.plan_sha256,
            selected_probes=("boot",),
            outcomes=(
                probe.ProbeOutcome(
                    0,
                    "boot",
                    passed,
                    None if passed else 5,
                    "boot-ok" if passed else "uname-failed",
                ),
            ),
            elapsed_seconds=2.5,
            recovered=True,
        )

    def test_success_publishes_private_hash_valid_result_last(self) -> None:
        publisher = probe.RealProbePublisher(self.directory)
        self.addCleanup(publisher.close)
        publisher.invalidate()

        publisher.publish(
            self._result(),
            b"context\nASTERINAS_PROBE_READY v=1 pid=1\n",
            b"",
        )

        names = {path.name for path in self.directory.iterdir()}
        self.assertEqual(names, {"result.json", "serial-summary.log", "sha256sums.txt"})
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        result = json.loads((self.directory / "result.json").read_bytes())
        self.assertTrue(result["passed"])
        for line in (self.directory / "sha256sums.txt").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(
                hashlib.sha256((self.directory / name).read_bytes()).hexdigest(),
                digest,
            )
        self.assertIn(
            "  result.json\n", (self.directory / "sha256sums.txt").read_text()
        )

    def test_failure_retains_only_bounded_dmesg_and_compact_serial(self) -> None:
        publisher = probe.RealProbePublisher(self.directory)
        self.addCleanup(publisher.close)
        publisher.invalidate()
        transcript = (
            b"x" * 10000
            + b"\nASTERINAS_PROBE_READY v=1 pid=1\n"
            + b"ordinary kernel noise\n" * 10000
            + b"ASTERINAS_PROBE_REBOOT_READY v=1 nonce="
            + b"0" * 32
            + b"\n"
        )

        publisher.publish(self._result(passed=False), transcript, b"failure\n")

        self.assertEqual(
            (self.directory / "failure.dmesg.log").read_bytes(), b"failure\n"
        )
        self.assertLessEqual(
            (self.directory / "serial-summary.log").stat().st_size, 8 * 1024
        )

    def test_stale_result_is_invalidated_before_new_work(self) -> None:
        self.directory.mkdir(mode=0o700)
        for name in probe.RealProbePublisher.OUTPUT_NAMES:
            (self.directory / name).write_text("stale")

        publisher = probe.RealProbePublisher(self.directory)
        self.addCleanup(publisher.close)
        publisher.invalidate()

        self.assertEqual(list(self.directory.iterdir()), [])

    def test_mid_publication_failure_never_leaves_terminal_result(self) -> None:
        publisher = probe.RealProbePublisher(self.directory)
        self.addCleanup(publisher.close)
        publisher.invalidate()
        original = probe.PinnedOutputDirectory.atomic_write

        def fail_on_manifest(output, name, contents, *, mode=0o600):
            if name == "sha256sums.txt":
                raise OSError("injected manifest failure")
            return original(output, name, contents, mode=mode)

        with (
            mock.patch.object(
                probe.PinnedOutputDirectory,
                "atomic_write",
                autospec=True,
                side_effect=fail_on_manifest,
            ),
            self.assertRaisesRegex(OSError, "injected"),
        ):
            publisher.publish(self._result(), b"serial\n", b"")

        self.assertFalse((self.directory / "result.json").exists())

    def test_symlink_destination_is_replaced_without_following(self) -> None:
        publisher = probe.RealProbePublisher(self.directory)
        self.addCleanup(publisher.close)
        publisher.invalidate()
        sentinel = Path(self.temporary_directory.name) / "sentinel"
        sentinel.write_text("unchanged")
        (self.directory / "serial-summary.log").symlink_to(sentinel)

        publisher.publish(self._result(), b"serial\n", b"")

        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertFalse((self.directory / "serial-summary.log").is_symlink())

    def test_output_directory_is_exclusively_locked_for_one_run(self) -> None:
        first = probe.RealProbePublisher(self.directory)
        self.addCleanup(first.close)
        first.invalidate()

        with self.assertRaisesRegex(probe.ProbeContractError, "already active"):
            probe.RealProbePublisher(self.directory)


class _PhysicalSerial:
    def __init__(self, _fd, *, max_bytes, tx_delay):
        self.max_bytes = max_bytes
        self.tx_delay = tx_delay
        self.sent: list[bytes] = []
        self.wait_for_any_calls: list[tuple[tuple[bytes, ...], float, int]] = []
        self._transcript = bytearray(b"kernel noise\nASTERINAS_PROBE_READY v=1 pid=1\n")

    @property
    def transcript(self) -> bytes:
        return bytes(self._transcript)

    def checkpoint(self) -> int:
        return len(self._transcript)

    def send(self, payload: bytes, deadline: float) -> None:
        if deadline <= time.monotonic():
            raise AssertionError("expired serial deadline")
        self.sent.append(payload)
        if payload.startswith(b"ASTERINAS_PROBE_RUN"):
            fields = dict(
                token.split("=", 1) for token in payload.decode().strip().split()[2:]
            )
            nonce = fields["nonce"]
            names = fields["probes"].split(",")
            for sequence, name in enumerate(names):
                detail = "boot-ok" if name == "boot" else "enosys"
                self._transcript.extend(
                    (
                        f"ASTERINAS_PROBE_START v=1 nonce={nonce} seq={sequence} name={name}\n"
                        f"ASTERINAS_PROBE_PASS v=1 nonce={nonce} seq={sequence} name={name} detail={detail}\n"
                    ).encode()
                )
            self._transcript.extend(
                (
                    f"ASTERINAS_PROBE_DONE v=1 nonce={nonce} count={len(names)} status=pass\n"
                    f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}\n"
                ).encode()
            )
        elif payload.startswith(b"ASTERINAS_PROBE_REBOOT"):
            self._transcript.extend(
                b"OpenSBI v1.5\nU-Boot 2024.01\nHit any key to stop autoboot: 30"
            )
        elif payload == b"\n":
            self._transcript.extend(b"\n=> ")

    def wait_for(self, marker: bytes, deadline: float, *, start: int = 0) -> bytes:
        if deadline <= time.monotonic() or self.transcript.find(marker, start) < 0:
            raise TimeoutError(f"missing marker: {marker!r}")
        return self.transcript

    def wait_for_any(self, markers, deadline: float, *, start: int = 0) -> bytes:
        candidates = tuple(markers)
        self.wait_for_any_calls.append((candidates, deadline, start))
        if deadline <= time.monotonic() or not any(
            self.transcript.find(marker, start) >= 0 for marker in candidates
        ):
            raise TimeoutError(f"missing markers: {candidates!r}")
        return next(
            marker for marker in candidates if self.transcript.find(marker, start) >= 0
        )


class PhysicalProbeOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = probe.ProbeBundle.from_bytes(_encoded(_bundle_mapping()))
        self.session = mock.Mock()
        self.session.wait_for_uboot_prompt.return_value = "U-Boot 2024.01\n=> "
        identities = {item.name: item for item in self.bundle.plan.artifacts}
        self.session.load_artifact.side_effect = lambda name, *_args: identities[
            name
        ].size
        self.serial_instances: list[_PhysicalSerial] = []
        self.closed: list[int] = []

        def serial_factory(*args, **kwargs):
            serial = _PhysicalSerial(*args, **kwargs)
            self.serial_instances.append(serial)
            return serial

        self.operations = probe.PhysicalProbeOperations(
            self.bundle,
            open_device=lambda _device: 41,
            lock_device=lambda fd: self.assertEqual(fd, 41),
            close_device=self.closed.append,
            session_factory=lambda *args, **kwargs: self.session,
            serial_factory=serial_factory,
        )

    def test_physical_adapter_loads_only_three_mmc_artifacts_and_recovers(self) -> None:
        self.operations.open(10)
        self.operations.ensure_artifacts(10)
        bootargs = probe.probe_bootargs(self.bundle.plan, 90)
        self.operations.boot(bootargs, 10)
        selected = probe.validate_probe_names(("boot", "syscall213"))
        nonce = "0" * 32
        exchange = self.operations.exchange(nonce, selected, False, 10)
        self.operations.request_reboot(nonce, 10)
        self.operations.await_recovery(10)
        self.operations.close()

        self.assertTrue(exchange.passed)
        self.assertEqual(
            self.session.load_artifact.call_args_list,
            [
                mock.call(
                    item.name,
                    MMC_PATHS[item.name],
                    item.load_address,
                    item.crc32,
                )
                for item in self.bundle.plan.artifacts
                if item.name in ("kernel", "initramfs", "megrez_dtb")
            ],
        )
        commands = [call.args[0] for call in self.session.command.call_args_list]
        command_text = "\n".join(commands)
        self.assertIn("mmc dev 1", commands)
        self.assertIn("mmc rescan", commands)
        self.assertNotIn("saveenv", command_text)
        self.assertNotIn("simple-framebuffer", command_text)
        self.assertNotIn("asterinas,usb-host", command_text)
        self.assertNotIn("asterinas.net=", command_text)
        self.session.start_boot_attempt.assert_called_once_with()
        self.assertTrue(
            any(
                call.args[0].startswith("booti ")
                for call in self.session.send.call_args_list
            )
        )
        self.assertEqual(self.serial_instances[0].sent[-1], b"\n")
        self.assertEqual(len(self.serial_instances[0].wait_for_any_calls), 2)
        self.assertEqual(self.closed, [41])

    def test_artifact_size_mismatch_fails_before_boot(self) -> None:
        self.operations.open(10)
        self.session.load_artifact.side_effect = lambda *_args: 1

        with self.assertRaisesRegex(probe.ProbeContractError, "size mismatch"):
            self.operations.ensure_artifacts(10)

    def test_interactive_shell_prints_complete_guest_responses(self) -> None:
        nonce = "0" * 32

        class ShellSerial:
            def __init__(self) -> None:
                self._transcript = bytearray(
                    f"ASTERINAS_PROBE_SHELL_READY v=1 nonce={nonce}\n".encode()
                )

            @property
            def transcript(self) -> bytes:
                return bytes(self._transcript)

            def checkpoint(self) -> int:
                return len(self._transcript)

            def send(self, payload: bytes, _deadline: float) -> None:
                if payload == b"help\n":
                    self._transcript.extend(
                        b"ASTERINAS_PROBE_SHELL_COMMANDS help,dmesg,mounts,boot,"
                        b"syscall213,syscall272,ext2-writeback,systemd-compat,exit\n"
                    )
                elif payload == b"exit\n":
                    self._transcript.extend(
                        f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}\n".encode()
                    )

            def wait_for(
                self, marker: bytes, _deadline: float, *, start: int = 0
            ) -> bytes:
                if self.transcript.find(marker, start) < 0:
                    raise TimeoutError(marker)
                return self.transcript

            def wait_for_any(
                self, markers, _deadline: float, *, start: int = 0
            ) -> bytes:
                if not any(
                    self.transcript.find(marker, start) >= 0 for marker in markers
                ):
                    raise TimeoutError(markers)
                return self.transcript

        self.operations._serial = ShellSerial()
        with tempfile.TemporaryFile(mode="w+") as input_file:
            input_file.write("help\nexit\n")
            input_file.seek(0)
            output = io.StringIO()
            with (
                mock.patch.object(probe.sys, "stdin", input_file),
                redirect_stdout(output),
            ):
                self.operations._interactive_shell(
                    self.operations._serial, time.monotonic() + 10
                )

        self.assertIn("ASTERINAS_PROBE_SHELL_COMMANDS", output.getvalue())

    def test_interactive_shell_detects_an_idle_reboot_before_forwarding_input(
        self,
    ) -> None:
        nonce = "0" * 32
        serial_read, serial_write = os.pipe()
        input_read, input_write = os.pipe()
        serial = probe.SerialConsole(serial_read, max_bytes=4096)
        os.write(
            serial_write,
            f"ASTERINAS_PROBE_SHELL_READY v=1 nonce={nonce}\n".encode(),
        )

        def emit_recovery() -> None:
            time.sleep(0.02)
            os.write(serial_write, b"OpenSBI v1.5\n")

        writer = threading.Thread(target=emit_recovery)
        writer.start()
        try:
            with os.fdopen(input_read, "r", closefd=False) as input_file:
                with (
                    mock.patch.object(probe.sys, "stdin", input_file),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        probe.ProbeProtocolError, "rebooted during probe shell"
                    ),
                ):
                    self.operations._interactive_shell(
                        serial, time.monotonic() + 1.0, nonce
                    )
        finally:
            writer.join(timeout=1)
            os.close(serial_read)
            os.close(serial_write)
            os.close(input_read)
            os.close(input_write)

        self.assertTrue(self.operations._guest_recovered_early)
        self.assertGreaterEqual(self.operations._recovery_cursor, 0)

    def test_interactive_shell_detects_an_already_buffered_reboot(self) -> None:
        nonce = "0" * 32
        serial_read, serial_write = os.pipe()
        serial = probe.SerialConsole(serial_read, max_bytes=4096)
        serial._append(
            (
                f"ASTERINAS_PROBE_SHELL_READY v=1 nonce={nonce}\n"
                "OpenSBI v1.5\nU-Boot 2024.01\n=> "
            ).encode()
        )
        sent: list[bytes] = []
        serial.send = lambda payload, _deadline: sent.append(payload)
        try:
            with tempfile.TemporaryFile(mode="w+") as input_file:
                input_file.write("boot\n")
                input_file.seek(0)
                with (
                    mock.patch.object(probe.sys, "stdin", input_file),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        probe.ProbeProtocolError, "rebooted during probe shell"
                    ),
                ):
                    self.operations._interactive_shell(
                        serial, time.monotonic() + 1.0, nonce
                    )
        finally:
            os.close(serial_read)
            os.close(serial_write)

        self.assertEqual(sent, [])
        self.assertTrue(self.operations._guest_recovered_early)

    def test_interactive_shell_partial_input_cannot_bypass_deadline(self) -> None:
        nonce = "0" * 32
        serial_read, serial_write = os.pipe()
        input_read, input_write = os.pipe()
        serial = probe.SerialConsole(serial_read, max_bytes=4096)
        serial._append(f"ASTERINAS_PROBE_SHELL_READY v=1 nonce={nonce}\n".encode())
        errors: list[BaseException] = []

        def run_shell() -> None:
            try:
                with os.fdopen(input_read, "r", closefd=False) as input_file:
                    with (
                        mock.patch.object(probe.sys, "stdin", input_file),
                        redirect_stdout(io.StringIO()),
                    ):
                        self.operations._interactive_shell(
                            serial, time.monotonic() + 0.1, nonce
                        )
            except BaseException as error:
                errors.append(error)

        os.write(input_write, b"bo")
        worker = threading.Thread(target=run_shell)
        worker.start()
        worker.join(timeout=0.5)
        blocked = worker.is_alive()
        os.close(input_write)
        worker.join(timeout=1.0)
        os.close(input_read)
        os.close(serial_read)
        os.close(serial_write)

        self.assertFalse(blocked, "partial shell input bypassed the deadline")
        self.assertTrue(any(isinstance(error, TimeoutError) for error in errors))

    def test_complete_transcript_rejects_records_hidden_after_done(self) -> None:
        nonce = "0" * 32
        transcript = (
            "ASTERINAS_PROBE_READY v=1 pid=1\n"
            f"ASTERINAS_PROBE_START v=1 nonce={nonce} seq=0 name=boot\n"
            f"ASTERINAS_PROBE_PASS v=1 nonce={nonce} seq=0 name=boot detail=boot-ok\n"
            f"ASTERINAS_PROBE_DONE v=1 nonce={nonce} count=1 status=pass\n"
            f"ASTERINAS_PROBE_DONE v=1 nonce={nonce} count=1 status=pass\n"
            f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={nonce}\n"
        ).encode()

        evidence = self.operations._classification_transcript(transcript, nonce)
        with self.assertRaises(probe.ProbeProtocolError):
            probe.classify_probe_transcript(evidence, nonce, ("boot",))

    def test_boot_uses_the_bundle_dtb_load_address(self) -> None:
        artifacts = tuple(
            replace(item, load_address=0xF1000000)
            if item.name == "megrez_dtb"
            else item
            for item in self.bundle.plan.artifacts
        )
        plan = replace(self.bundle.plan, artifacts=artifacts)
        bundle = replace(self.bundle, plan=plan, plan_sha256=plan.plan_sha256)
        self.operations._bundle = bundle
        self.operations.open(10)
        self.operations.ensure_artifacts(10)

        self.operations.boot(probe.probe_bootargs(plan, 90), 10)

        commands = [call.args[0] for call in self.session.command.call_args_list]
        self.assertIn("fdt addr 0xf1000000", commands)


class ProbeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.plan_path = self.directory / "plan.json"
        self.plan_path.write_bytes(_plan().canonical_bytes())
        self.bundle_path = self.directory / "current.json"

    def test_normal_cli_needs_only_probe_names(self) -> None:
        values = probe.parse_args(("boot", "syscall213"))

        self.assertEqual(values.action, "run")
        self.assertEqual(values.probes, ("boot", "syscall213"))
        self.assertEqual(values.session_seconds, 90)
        self.assertEqual(values.bundle, Path("target/megrez-probe/current.json"))
        self.assertEqual(values.output_directory, Path("target/megrez-probe/latest"))

    def test_cli_rejects_unbounded_shell_before_operations_exist(self) -> None:
        for seconds in (29, 301):
            with (
                self.subTest(seconds=seconds),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                probe.parse_args(("boot", "--shell", "--session-seconds", str(seconds)))

    def test_qemu_cli_requires_both_explicit_artifact_paths(self) -> None:
        values = probe.parse_args(
            (
                "boot",
                "--qemu",
                "--qemu-kernel",
                "/kernel",
                "--qemu-initramfs",
                "/initramfs",
            )
        )

        self.assertTrue(values.qemu)
        self.assertEqual(values.qemu_kernel, Path("/kernel"))
        self.assertEqual(values.qemu_initramfs, Path("/initramfs"))
        for arguments in (
            ("boot", "--qemu"),
            ("boot", "--qemu-kernel", "/kernel"),
            ("boot", "--qemu-deadline-only"),
        ):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    probe.parse_args(arguments)

    def test_qemu_cli_rejects_the_physical_only_shell_before_launch(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            probe.parse_args(
                (
                    "boot",
                    "--qemu",
                    "--qemu-kernel",
                    "/kernel",
                    "--qemu-initramfs",
                    "/initramfs",
                    "--shell",
                )
            )

    def test_configure_atomically_writes_private_current_bundle(self) -> None:
        with redirect_stdout(io.StringIO()):
            result = probe.main(
                (
                    "configure",
                    "--plan",
                    str(self.plan_path),
                    "--device",
                    SERIAL_DEVICE,
                    "--mmc-kernel",
                    MMC_PATHS["kernel"],
                    "--mmc-initramfs",
                    MMC_PATHS["initramfs"],
                    "--mmc-dtb",
                    MMC_PATHS["megrez_dtb"],
                    "--bundle",
                    str(self.bundle_path),
                )
            )

        self.assertEqual(result, 0)
        bundle = probe.ProbeBundle.from_bytes(self.bundle_path.read_bytes())
        self.assertEqual(bundle.plan_sha256, _plan().plan_sha256)
        self.assertEqual(stat.S_IMODE(self.bundle_path.stat().st_mode), 0o600)

    def test_configure_does_not_change_an_existing_parent_mode(self) -> None:
        parent = self.directory / "existing-parent"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        bundle_path = parent / "current.json"

        with redirect_stdout(io.StringIO()):
            result = probe.main(
                (
                    "configure",
                    "--plan",
                    str(self.plan_path),
                    "--device",
                    SERIAL_DEVICE,
                    "--mmc-kernel",
                    MMC_PATHS["kernel"],
                    "--mmc-initramfs",
                    MMC_PATHS["initramfs"],
                    "--mmc-dtb",
                    MMC_PATHS["megrez_dtb"],
                    "--bundle",
                    str(bundle_path),
                )
            )

        self.assertEqual(result, 0)
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)

    def test_run_cli_uses_one_operations_instance(self) -> None:
        bundle = probe.ProbeBundle.from_bytes(_encoded(_bundle_mapping()))
        self.bundle_path.write_bytes(bundle.canonical_bytes())
        output = self.directory / "result"
        events: list[str] = []
        operations = _LifecycleOperations(
            events,
            probe.ProbeExchange(
                (probe.ProbeOutcome(0, "boot", True, None, "boot-ok"),),
                True,
                b"",
            ),
        )
        factory = mock.Mock(return_value=operations)

        with redirect_stdout(io.StringIO()):
            result = probe.main(
                (
                    "boot",
                    "--bundle",
                    str(self.bundle_path),
                    "--output-directory",
                    str(output),
                ),
                operations_factory=factory,
                stdin_isatty=lambda: False,
            )

        self.assertEqual(result, 0)
        factory.assert_called_once_with(bundle)
        self.assertTrue(json.loads((output / "result.json").read_text())["passed"])

    def test_bounded_reader_rejects_fifo_without_waiting_for_a_writer(self) -> None:
        fifo = self.directory / "bundle.fifo"
        os.mkfifo(fifo)
        script = (
            "from pathlib import Path; "
            "from tools.riscv.megrez_probe import _read_bounded_regular; "
            f"_read_bounded_regular(Path({str(fifo)!r}), 1024, 'bundle')"
        )
        process = __import__("subprocess").Popen(
            ["python3", "-c", script], cwd=Path(__file__).resolve().parents[3]
        )
        try:
            self.assertNotEqual(process.wait(timeout=1), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class _QemuProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, _deadline):
        self.returncode = 0
        return 0

    def terminate_group(self, _term_deadline, _kill_deadline):
        self.terminated = True
        self.returncode = -15


class _DeadlineOperations:
    def __init__(self, clock, *, fatal: bool = False) -> None:
        self.events = []
        self._guest_started = False
        self.clock = clock
        self.fatal = fatal

    @property
    def guest_started(self):
        return self._guest_started

    @property
    def transcript(self):
        fatal = b"Kernel panic - not syncing\n" if self.fatal else b""
        return (
            b"ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=30\n"
            b"ASTERINAS_PROBE_READY v=1 pid=1\n" + fatal
        )

    def open(self, _timeout):
        self.events.append("open")

    def ensure_artifacts(self, _timeout):
        self.events.append("artifacts")
        return ("kernel:qemu", "initramfs:qemu")

    def boot(self, bootargs, _timeout):
        self.events.append(("boot", bootargs))
        self._guest_started = True

    def await_ready(self, _timeout):
        self.events.append("ready")

    def await_recovery(self, _timeout):
        self.events.append("recovered")
        self.clock.now += 1.0 if self.fatal else 30.0

    def close(self):
        self.events.append("close")


class QemuProbeOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        addresses = {
            "kernel": 0x80200000,
            "initramfs": 0x83000000,
            "qemu_dtb": 0xF0000000,
            "megrez_dtb": 0xF0000000,
        }
        artifacts = []
        for name in ("kernel", "initramfs", "qemu_dtb", "megrez_dtb"):
            path = self.directory / name
            path.write_bytes((name + "-bytes").encode())
            artifacts.append(ArtifactIdentity.from_path(name, path, addresses[name]))
        plan = DebugPlan(
            schema_version=1,
            profile="tcp-probe",
            artifacts=tuple(artifacts),
            bootargs="loglevel=info init=/init asterinas.reboot_after=180",
            smp=4,
            sv39=True,
            markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
            reboot_after=180,
        )
        self.bundle = probe.ProbeBundle(
            schema_version=1,
            plan=plan,
            plan_sha256=plan.plan_sha256,
            device=SERIAL_DEVICE,
            mmc_artifacts=tuple(
                probe.MmcArtifact(name, MMC_PATHS[name])
                for name in ("kernel", "initramfs", "megrez_dtb")
            ),
        )

    def test_qemu_argv_is_minimal_sv39_and_has_no_external_device(self) -> None:
        argv = probe.qemu_probe_argv(
            Path("/proc/self/fd/10"),
            Path("/proc/self/fd/11"),
            probe.probe_bootargs(self.bundle.plan, 90),
        )

        self.assertEqual(argv[0], "qemu-system-riscv64")
        self.assertEqual(argv[argv.index("-machine") + 1], "virt")
        self.assertEqual(argv[argv.index("-m") + 1], "2G")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertIn("-nographic", argv)
        self.assertIn("-no-reboot", argv)
        self.assertIn("-kernel", argv)
        self.assertIn("-initrd", argv)
        self.assertNotIn("-drive", argv)
        self.assertNotIn("-netdev", argv)
        self.assertNotIn("-device", argv)

    def test_qemu_adapter_pins_identities_and_launches_the_same_probe_bootargs(
        self,
    ) -> None:
        launched = []
        process = _QemuProcess()

        def launcher(argv, **kwargs):
            launched.append((tuple(argv), kwargs))
            return process

        operations = probe.QemuProbeOperations(
            self.bundle,
            self.directory / "kernel",
            self.directory / "initramfs",
            launch=launcher,
        )
        operations.open(5)
        self.assertEqual(
            operations.ensure_artifacts(5), ("kernel:qemu", "initramfs:qemu")
        )
        bootargs = probe.probe_bootargs(self.bundle.plan, 90)
        operations.boot(bootargs, 5)
        operations.close()

        self.assertEqual(len(launched), 1)
        argv, kwargs = launched[0]
        self.assertEqual(argv[argv.index("-append") + 1], bootargs)
        self.assertEqual(len(kwargs["pass_fds"]), 2)
        self.assertTrue(process.terminated)

    def test_qemu_recovery_drains_the_pty_while_waiting_for_exit(self) -> None:
        master, slave = os.openpty()
        process = probe.launch_process(
            (
                sys.executable,
                "-c",
                "import os; os.write(1, b'x' * 131072)",
            ),
            stdio_fd=slave,
        )
        os.close(slave)
        operations = object.__new__(probe.QemuProbeOperations)
        operations._process = process
        operations._serial = probe.SerialConsole(
            master, process=process, max_bytes=256 * 1024
        )
        try:
            operations.await_recovery(2.0)
            self.assertEqual(len(operations.transcript), 131072)
        finally:
            if process.poll() is None:
                process.terminate_group(time.monotonic() + 1, time.monotonic() + 2)
            os.close(master)

    def test_qemu_adapter_rejects_changed_artifact_before_launch(self) -> None:
        kernel = self.directory / "kernel"
        kernel.write_bytes(b"changed")
        operations = probe.QemuProbeOperations(
            self.bundle,
            kernel,
            self.directory / "initramfs",
        )
        operations.open(5)
        self.addCleanup(operations.close)

        with self.assertRaisesRegex(probe.ProbeContractError, "identity mismatch"):
            operations.ensure_artifacts(5)

    def test_deadline_gate_sends_no_probe_request_and_requires_process_exit(
        self,
    ) -> None:
        clock = SimpleNamespace(now=100.0)
        operations = _DeadlineOperations(clock)
        publisher = _LifecyclePublisher([])

        result = probe.run_qemu_deadline_gate(
            self.bundle,
            operations,
            publisher,
            clock=lambda: clock.now,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "deadline-reboot-pass")
        self.assertEqual(
            [
                event if isinstance(event, str) else event[0]
                for event in operations.events
            ],
            ["open", "artifacts", "boot", "ready", "recovered", "close"],
        )
        bootargs = operations.events[2][1]
        self.assertIn("asterinas.reboot_after=30", bootargs)
        self.assertNotIn("ASTERINAS_PROBE_RUN", operations.transcript.decode())

    def test_deadline_gate_rejects_an_immediate_fatal_restart(self) -> None:
        clock = SimpleNamespace(now=100.0)
        operations = _DeadlineOperations(clock, fatal=True)
        publisher = _LifecyclePublisher([])

        result = probe.run_qemu_deadline_gate(
            self.bundle,
            operations,
            publisher,
            clock=lambda: clock.now,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "deadline-failed")
        self.assertTrue(result.recovered)

    def test_deadline_gate_publishes_serial_overflow_as_failure(self) -> None:
        clock = SimpleNamespace(now=100.0)

        class OverflowOperations(_DeadlineOperations):
            def await_recovery(self, _timeout):
                raise BufferError("serial transcript exceeds byte cap")

        operations = OverflowOperations(clock)
        publisher = _LifecyclePublisher([])

        result = probe.run_qemu_deadline_gate(
            self.bundle,
            operations,
            publisher,
            clock=lambda: clock.now,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "manual-reset-required")
        self.assertIsNotNone(publisher.published)


if __name__ == "__main__":
    unittest.main()
