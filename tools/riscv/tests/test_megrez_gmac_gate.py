#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded tests for the physical Megrez wired-network gate."""

from __future__ import annotations

import subprocess
import signal
import unittest
from collections.abc import Iterable

from tools.riscv.megrez_gmac_gate import (
    BOARD_ADDRESS,
    HOST_PING_ARGV,
    PHYSICAL_MILESTONES,
    GateConfig,
    GateFailure,
    GateTermination,
    check_address_unused,
    classify_physical_transcript,
    physical_bootargs,
    run_gate,
    verify_host_ping,
)


class FakeOperations:
    def __init__(
        self,
        chunks: Iterable[bytes] = (),
        *,
        boot: bytes = b"",
        ping: subprocess.CompletedProcess[bytes] | None = None,
        conflict: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.chunks = iter(chunks)
        self.boot_output = boot
        self.ping = ping or subprocess.CompletedProcess(
            HOST_PING_ARGV,
            0,
            b"10 packets transmitted, 10 received, 0% packet loss\n",
            b"",
        )
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
        self.events.append("host-ping")
        return self.ping

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
        self.assertEqual(bootargs.split()[0:2], ["console=tty0", "console=ttyS0"])
        self.assertNotIn("saveenv", bootargs)
        self.assertNotIn("reboot_after", bootargs)
        recovery_bootargs = physical_bootargs(180)
        self.assertIn("asterinas.reboot_after=180", recovery_bootargs.split())
        self.assertNotIn("saveenv", recovery_bootargs)

    def test_address_conflict_is_rejected_before_serial_open(self) -> None:
        operations = FakeOperations(conflict=True)

        result = run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertFalse(result["passed"])
        self.assertEqual(
            operations.events,
            ["invalidate", "address-check", "publish"],
        )
        self.assertIsNotNone(operations.published)

    def test_split_markers_then_exact_ten_host_pings_pass(self) -> None:
        evidence = b"\n".join(PHYSICAL_MILESTONES) + b"\n"
        operations = FakeOperations(
            chunks=(evidence[11:73], evidence[73:], b"late harmless log\n"),
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
                "host-ping",
                "drain",
                "close",
                "publish",
            ],
        )
        self.assertEqual(result["host_ping_count"], 10)
        self.assertIn(b"late harmless log", operations.published[0])

    def test_failure_or_fatal_drain_closes_and_never_passes(self) -> None:
        evidence = b"\n".join(PHYSICAL_MILESTONES) + b"\n"
        operations = FakeOperations(
            chunks=(evidence, b"Kernel panic - not syncing\n"),
        )

        result = run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "fatal transcript marker: kernel panic")
        self.assertLess(
            operations.events.index("close"), operations.events.index("publish")
        )

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
        complete = b"\n".join(PHYSICAL_MILESTONES)
        self.assertTrue(classify_physical_transcript(complete).passed)
        cases = (
            (b"\n".join(PHYSICAL_MILESTONES[:-1]), "missing physical milestone"),
            (
                complete + b"\n" + PHYSICAL_MILESTONES[-1],
                "duplicate physical milestone",
            ),
            (
                b"\n".join(reversed(PHYSICAL_MILESTONES)),
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

    def test_address_probe_and_host_ping_have_strict_process_contracts(self) -> None:
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
        self.assertEqual(
            verify_host_ping(
                subprocess.CompletedProcess(
                    HOST_PING_ARGV,
                    0,
                    b"10 packets transmitted, 10 received, 0% packet loss\n",
                    b"",
                )
            ),
            10,
        )
        with self.assertRaisesRegex(GateFailure, "host ping failed"):
            verify_host_ping(
                subprocess.CompletedProcess(
                    HOST_PING_ARGV,
                    1,
                    b"10 packets transmitted, 9 received, 10% packet loss\n",
                    b"",
                )
            )


if __name__ == "__main__":
    unittest.main()
