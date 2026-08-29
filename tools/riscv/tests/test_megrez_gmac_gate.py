#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded tests for the physical Megrez wired-network gate."""

from __future__ import annotations

import subprocess
import signal
import unittest
from collections.abc import Iterable

from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_MEGREZ_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m6_browser_gate import (
    DESKTOP_M6_JAVASCRIPT_STATUSES,
    DESKTOP_M6_REMOTE_MARKER,
)
from tools.riscv.megrez_gmac_gate import (
    BOARD_ADDRESS,
    PHYSICAL_MILESTONES,
    _parse_args,
    GateConfig,
    GateFailure,
    GateTermination,
    check_address_unused,
    classify_physical_transcript,
    physical_bootargs,
    run_gate,
)


EXPECTED_PHYSICAL_MILESTONES = (
    b"ASTERINAS_GMAC_SELECTED key=eic7700-rj45 ",
    *(marker.encode() for marker in DESKTOP_M5_MEGREZ_MILESTONES),
    DESKTOP_M4_MILESTONES[-1].encode(),
    DESKTOP_M6_REMOTE_MARKER.encode(),
)


def complete_browser_evidence(status: str = "limited-pass") -> bytes:
    return (
        b"\n".join(
            (
                *EXPECTED_PHYSICAL_MILESTONES,
                f"DEBIAN_BROWSER_M6_JAVASCRIPT status={status}".encode(),
                f"DEBIAN_BROWSER_M6_READY remote=baidu javascript={status}".encode(),
            )
        )
        + b"\n"
    )


class FakeOperations:
    def __init__(
        self,
        chunks: Iterable[bytes] = (),
        *,
        boot: bytes = b"",
        conflict: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.chunks = iter(chunks)
        self.boot_output = boot
        self.conflict = conflict
        self.published: tuple[bytes, dict[str, object]] | None = None

    def invalidate(self) -> None:
        self.events.append("invalidate")

    def ensure_address_unused(self) -> None:
        self.events.append("address-check")
        if self.conflict:
            raise GateFailure("board IPv4 address is already in use")

    def open_board(self) -> None:
        self.events.append("open")

    def boot(self) -> bytes:
        self.events.append("boot")
        return self.boot_output

    def read(self, deadline: float) -> bytes:
        del deadline
        self.events.append("read")
        return next(self.chunks, b"")

    def ping_board(self) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("the browser network gate must not use ICMP")

    def drain(self, deadline: float) -> bytes:
        del deadline
        self.events.append("drain")
        return next(self.chunks, b"")

    def close_board(self) -> None:
        self.events.append("close")

    def publish(self, transcript: bytes, result: dict[str, object]) -> None:
        self.events.append("publish")
        self.published = transcript, result


class MegrezGmacGateTests(unittest.TestCase):
    def test_bootargs_bind_static_profile_without_persistent_uboot_writes(self) -> None:
        bootargs = physical_bootargs()

        self.assertIn(
            "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1",
            bootargs.split(),
        )
        self.assertIn(
            "asterinas.neighbor=eic7700-rj45,10.100.16.1,4c:d6:29:18:93:43",
            bootargs.split(),
        )
        self.assertIn(
            "asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e",
            bootargs.split(),
        )
        self.assertNotIn(
            "asterinas.neighbor=eic7700-rj45,10.100.16.28,d8:43:ae:b1:f8:12",
            bootargs.split(),
        )
        self.assertEqual(bootargs.split()[0:2], ["console=ttyS0", "console=tty0"])
        self.assertNotIn("cpu_no_boost_1_6ghz", bootargs.split())
        self.assertIn("asterinas.mmc_write_partition2", bootargs.split())
        for variable in (
            "ASTERINAS_DESKTOP_M4_CONSOLE",
            "ASTERINAS_DESKTOP_M5_CONSOLE",
            "ASTERINAS_BROWSER_M6_CONSOLE",
            "ASTERINAS_BROWSER_M7_CONSOLE",
        ):
            self.assertIn(
                f"systemd.setenv={variable}=/dev/ttyS0",
                bootargs.split(),
            )
        for variable, value in (
            ("ASTERINAS_DESKTOP_PROXY_URL", "http://10.100.19.216:17893"),
            ("ASTERINAS_DESKTOP_PROXY_HOST", "10.100.19.216"),
            ("ASTERINAS_DESKTOP_PROXY_PORT", "17893"),
        ):
            self.assertIn(f"systemd.setenv={variable}={value}", bootargs.split())
        self.assertNotIn("saveenv", bootargs)
        self.assertNotIn("reboot_after", bootargs)
        recovery_bootargs = physical_bootargs(180)
        self.assertIn("asterinas.reboot_after=180", recovery_bootargs.split())
        self.assertNotIn("saveenv", recovery_bootargs)

    def test_physical_gate_accepts_only_complete_ymodem_contract(self) -> None:
        required = [
            "/dev/ttyUSB0",
            "--booti",
            "kernel.lzma",
            "--initrd",
            "stage1.cpio",
            "--dtb",
            "board.dtb",
            "--expected-crc32",
            "booti=12345678,dtb=90abcdef,initrd=deadbeef",
            "--host-interface",
            "enp12s0",
            "--output-directory",
            "/tmp/gmac-gate",
            "--load-transport",
            "ymodem",
        ]
        with self.assertRaises(SystemExit):
            _parse_args(required)

        parsed = _parse_args(
            required
            + [
                "--ymodem-directory",
                "/tmp/transfer",
                "--booti-compressed-crc32",
                "13572468",
                "--booti-uncompressed-size",
                "14530072",
            ]
        )
        self.assertEqual(parsed.load_transport, "ymodem")
        self.assertEqual(parsed.booti_compressed_crc32, "13572468")
        self.assertEqual(parsed.booti_uncompressed_size, 14530072)

    def test_address_conflict_is_rejected_before_serial_open(self) -> None:
        operations = FakeOperations(conflict=True)

        result = run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertFalse(result["passed"])
        self.assertEqual(
            operations.events,
            ["invalidate", "address-check", "publish"],
        )
        self.assertIsNotNone(operations.published)

    def test_split_browser_network_markers_pass_without_icmp(self) -> None:
        self.assertEqual(PHYSICAL_MILESTONES, EXPECTED_PHYSICAL_MILESTONES)
        evidence = complete_browser_evidence()
        operations = FakeOperations(
            chunks=(
                evidence[11:173],
                evidence[173:-19],
                evidence[-19:],
                b"late harmless log\n",
            ),
            boot=evidence[:11],
        )

        result = run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertTrue(result["passed"], result)
        self.assertEqual(
            operations.events,
            [
                "invalidate",
                "address-check",
                "open",
                "boot",
                "read",
                "read",
                "read",
                "drain",
                "close",
                "publish",
            ],
        )
        self.assertNotIn("host_ping_count", result)
        self.assertEqual(result["javascript_status"], "limited-pass")
        self.assertIn(b"late harmless log", operations.published[0])

    def test_quiet_serial_interval_is_not_treated_as_disconnect(self) -> None:
        evidence = complete_browser_evidence()
        operations = FakeOperations(chunks=(b"", evidence, b""))

        result = run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertTrue(result["passed"], result)

    def test_failure_or_fatal_drain_closes_and_never_passes(self) -> None:
        evidence = complete_browser_evidence()
        operations = FakeOperations(
            chunks=(evidence, b"Kernel panic - not syncing\n"),
        )

        result = run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "fatal transcript marker: kernel panic")
        self.assertLess(
            operations.events.index("close"), operations.events.index("publish")
        )

    def test_stage1_failure_stops_the_boot_wait_immediately(self) -> None:
        operations = FakeOperations(
            chunks=(
                b"DEBIAN_ROOTFS_FAIL reason=root-init-argument\n",
                b"this second boot read must not happen\n",
            )
        )

        result = run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertFalse(result["passed"])
        self.assertEqual(
            result["reason"], "fatal transcript marker: Stage1 rootfs failure"
        )
        self.assertEqual(operations.events.count("read"), 1)

    def test_termination_unwinds_serial_without_publishing_result(self) -> None:
        class TerminatedOperations(FakeOperations):
            def read(self, deadline: float) -> bytes:
                del deadline
                self.events.append("read")
                raise GateTermination(signal.SIGTERM)

        operations = TerminatedOperations()

        with self.assertRaises(GateTermination):
            run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertIn("close", operations.events)
        self.assertNotIn("publish", operations.events)

    def test_classifier_rejects_missing_duplicate_reordered_and_guest_failure(
        self,
    ) -> None:
        complete = complete_browser_evidence()
        self.assertTrue(classify_physical_transcript(complete).passed)
        cases = (
            (
                complete.replace(EXPECTED_PHYSICAL_MILESTONES[-1], b""),
                "missing physical milestone",
            ),
            (
                complete + b"\n" + EXPECTED_PHYSICAL_MILESTONES[-1],
                "duplicate physical milestone",
            ),
            (
                b"\n".join(reversed(EXPECTED_PHYSICAL_MILESTONES))
                + b"\nDEBIAN_BROWSER_M6_JAVASCRIPT status=limited-pass"
                + b"\nDEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass",
                "physical milestones out of order",
            ),
            (
                complete + b"\nDEBIAN_NETWORK_M5_FAIL reason=guest-ping",
                "fatal transcript marker: guest network failure",
            ),
        )
        for transcript, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    classify_physical_transcript(transcript).reason, reason
                )

    def test_classifier_requires_one_matching_ordered_browser_result(self) -> None:
        for status in DESKTOP_M6_JAVASCRIPT_STATUSES:
            with self.subTest(status=status):
                self.assertTrue(
                    classify_physical_transcript(
                        complete_browser_evidence(status)
                    ).passed
                )

        complete = complete_browser_evidence()
        remote = DESKTOP_M6_REMOTE_MARKER.encode()
        ready = b"DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass"
        cases = (
            (
                complete + remote + b"\n",
                "duplicate physical milestone",
            ),
            (
                complete + ready + b"\n",
                "missing or duplicate browser ready evidence",
            ),
            (
                complete.replace(b"javascript=limited-pass", b"javascript=disabled"),
                "missing or mismatched browser ready evidence",
            ),
            (
                complete.replace(
                    b"DEBIAN_BROWSER_M6_JAVASCRIPT status=limited-pass\n",
                    b"",
                ),
                "missing or duplicate JavaScript evidence",
            ),
            (
                complete + b"DEBIAN_BROWSER_M6_FAIL reason=remote-window-timeout\n",
                "fatal transcript marker: browser guest failure",
            ),
        )
        for transcript, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    classify_physical_transcript(transcript).reason,
                    reason,
                )

    def test_address_probe_has_a_strict_process_contract(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run_unused(
            argv: tuple[str, ...], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append(argv)
            self.assertTrue(kwargs["capture_output"])
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        check_address_unused("enp1s0", BOARD_ADDRESS, run=run_unused)
        self.assertEqual(
            calls,
            [
                (
                    "arping",
                    "-D",
                    "-c",
                    "2",
                    "-w",
                    "3",
                    "-I",
                    "enp1s0",
                    BOARD_ADDRESS,
                )
            ],
        )

        conflict = subprocess.CompletedProcess(calls[0], 1, b"reply", b"")
        with self.assertRaisesRegex(GateFailure, "already in use"):
            check_address_unused(
                "enp1s0",
                BOARD_ADDRESS,
                run=lambda *args, **kwargs: conflict,
            )


if __name__ == "__main__":
    unittest.main()
