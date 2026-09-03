#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded tests for the physical Megrez wired-network gate."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import tempfile
import subprocess
import signal
import unittest
from unittest import mock
from collections.abc import Iterable
from pathlib import Path

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_CORE_MILESTONES,
    DESKTOP_M4_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_MEGREZ_MILESTONES,
    NETWORK_LAYERS,
    NetworkMode,
)
from tools.riscv.debian.rootfs.desktop_m6_browser_gate import (
    DESKTOP_M6_JAVASCRIPT_STATUSES,
    DESKTOP_M6_REMOTE_MARKER,
)
from tools.riscv.debian.rootfs.desktop_m7_baidu_gate import (
    DESKTOP_M7_HOME_MARKER,
    DESKTOP_M7_READY_MARKER,
    DESKTOP_M7_SEARCH_MARKER,
)
from tools.riscv.megrez_gmac_gate import (
    BOARD_ADDRESS,
    PHYSICAL_DESKTOP_MILESTONES,
    PHYSICAL_MILESTONES,
    PHYSICAL_M7_MILESTONES,
    _parse_args,
    GateConfig,
    GateFailure,
    GateTarget,
    GateTermination,
    PhysicalGateOperations,
    check_address_unused,
    classify_physical_desktop_transcript,
    classify_physical_network_transcript,
    classify_physical_transcript,
    physical_bootargs,
    run_gate,
)
from tools.riscv.megrez_network_fixture import (
    BROWSER_IMAGE,
    BROWSER_PNG_CAPTURE_PATH,
    FIXTURE_PATH,
    PAYLOAD_SHA256,
    PAYLOAD_SIZE,
    FixtureConfig,
    FixtureServer,
)
from tools.riscv import megrez_gmac_gate as gmac_gate


EXPECTED_PHYSICAL_MILESTONES = (
    b"ASTERINAS_GMAC_SELECTED key=eic7700-rj45 ",
    *(marker.encode() for marker in DESKTOP_M5_MEGREZ_MILESTONES),
    DESKTOP_M4_MILESTONES[-1].encode(),
    DESKTOP_M6_REMOTE_MARKER.encode(),
)
EXPECTED_PHYSICAL_NETWORK_MILESTONES = (
    b"ASTERINAS_GMAC_SELECTED key=eic7700-rj45 ",
    *(marker.encode() for marker in DESKTOP_M5_MEGREZ_MILESTONES),
)
EXPECTED_PHYSICAL_M7_MILESTONES = (
    DESKTOP_M7_HOME_MARKER.encode(),
    DESKTOP_M7_SEARCH_MARKER.encode(),
    DESKTOP_M7_READY_MARKER.encode(),
)


def complete_browser_evidence(status: str = "limited-pass") -> bytes:
    return (
        b"\n".join(
            (
                *EXPECTED_PHYSICAL_MILESTONES,
                f"DEBIAN_BROWSER_M6_JAVASCRIPT status={status}".encode(),
                f"DEBIAN_BROWSER_M6_READY remote=baidu javascript={status}".encode(),
                *EXPECTED_PHYSICAL_M7_MILESTONES,
            )
        )
        + b"\n"
    )


def complete_network_evidence() -> bytes:
    return b"\n".join(EXPECTED_PHYSICAL_NETWORK_MILESTONES) + b"\n"


def complete_desktop_evidence() -> bytes:
    return b"\n".join(marker.encode() for marker in DESKTOP_M4_CORE_MILESTONES) + b"\n"


def complete_web_network_evidence(mode: NetworkMode) -> bytes:
    records = [b"ASTERINAS_GMAC_SELECTED key=eic7700-rj45 port=0"]
    records.extend(
        (
            f"DEBIAN_WEB_NETWORK_LAYER mode={mode.value} "
            f"layer={layer} status=pass"
        ).encode()
        for layer in NETWORK_LAYERS
    )
    records.append(
        f"DEBIAN_WEB_NETWORK_READY mode={mode.value} layers=10".encode()
    )
    return b"\n".join(records) + b"\n"


class FakeOperations:
    def __init__(
        self,
        chunks: Iterable[bytes] = (),
        *,
        boot: bytes = b"",
        conflict: bool = False,
        recovery: bytes = b"OpenSBI v1.7\nU-Boot 2025.01\n=> ",
    ) -> None:
        self.events: list[str] = []
        self.chunks = iter(chunks)
        self.boot_output = boot
        self.conflict = conflict
        self.recovery = recovery
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

    def wait_for_recovery(self, deadline: float) -> bytes:
        del deadline
        self.events.append("recovery")
        return self.recovery

    def close_board(self) -> None:
        self.events.append("close")

    def publish(self, transcript: bytes, result: dict[str, object]) -> None:
        self.events.append("publish")
        self.published = transcript, result


class FakeProxyBridge:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.events: list[str] = []
        self.running = False
        self.closed = False

    def start(self) -> "FakeProxyBridge":
        self.events.append("start")
        if self.fail_start:
            raise RuntimeError("bridge start failed")
        self.running = True
        return self

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.events.append("close")
        self.running = False

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "listen": "10.100.19.216:17893",
            "upstream": "127.0.0.1:17892",
            "pid": 42,
            "ready": self.running,
            "exit_status": None if self.running else 0,
            "stderr_hex": "",
        }


class MegrezGmacGateTests(unittest.TestCase):
    def test_web_targets_require_explicit_network_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a NetworkMode"):
            GateConfig(target=GateTarget.NETWORK)
        with self.assertRaisesRegex(ValueError, "requires a NetworkMode"):
            GateConfig(target=GateTarget.FIREFOX)
        with self.assertRaisesRegex(ValueError, "requires network or firefox"):
            GateConfig(target=GateTarget.DESKTOP, network_mode=NetworkMode.PROXY)

        proxy = GateConfig(
            target=GateTarget.NETWORK,
            network_mode=NetworkMode.PROXY,
        )
        direct = GateConfig(
            target=GateTarget.FIREFOX,
            network_mode=NetworkMode.DIRECT,
        )
        self.assertEqual(proxy.network_mode, NetworkMode.PROXY)
        self.assertEqual(direct.network_mode, NetworkMode.DIRECT)

    def test_web_bootargs_are_mode_specific_and_fit_uboot(self) -> None:
        self.assertIn("network_mode", inspect.signature(physical_bootargs).parameters)
        proxy = physical_bootargs(
            600,
            target=GateTarget.FIREFOX,
            network_mode=NetworkMode.PROXY,
        )
        direct = physical_bootargs(
            600,
            target=GateTarget.FIREFOX,
            network_mode=NetworkMode.DIRECT,
            resolver_address="10.100.16.1",
        )

        self.assertIn(
            "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy",
            proxy.split(),
        )
        self.assertIn("ASTERINAS_DESKTOP_PROXY_URL=", proxy)
        self.assertIn(
            "systemd.setenv=ASTERINAS_BROWSER_WEB_CONSOLE=/dev/ttyS0",
            proxy.split(),
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=direct",
            direct.split(),
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_WEB_NETWORK_RESOLVER=10.100.16.1",
            direct.split(),
        )
        self.assertNotIn("ASTERINAS_DESKTOP_PROXY_", direct)
        for bootargs in (proxy, direct):
            self.assertLess(len(f'setenv bootargs "{bootargs}"'.encode()), 1024)
            self.assertNotIn("saveenv", bootargs)

    def test_physical_web_network_classifier_isolates_modes(self) -> None:
        self.assertTrue(
            hasattr(gmac_gate, "classify_physical_web_network_transcript")
        )
        proxy = complete_web_network_evidence(NetworkMode.PROXY)
        direct = complete_web_network_evidence(NetworkMode.DIRECT)
        self.assertTrue(
            gmac_gate.classify_physical_web_network_transcript(
                proxy, mode=NetworkMode.PROXY
            ).passed
        )
        self.assertTrue(
            gmac_gate.classify_physical_web_network_transcript(
                direct, mode=NetworkMode.DIRECT
            ).passed
        )
        self.assertFalse(
            gmac_gate.classify_physical_web_network_transcript(
                proxy, mode=NetworkMode.DIRECT
            ).passed
        )

    def test_web_network_gate_publishes_requested_mode(self) -> None:
        self.assertIn("network_mode", inspect.signature(GateConfig).parameters)
        evidence = complete_web_network_evidence(NetworkMode.PROXY)
        operations = FakeOperations(chunks=(evidence, b"late harmless log\n"))

        result = run_gate(
            GateConfig(
                boot_timeout=1,
                drain_timeout=1,
                target=GateTarget.NETWORK,
                network_mode=NetworkMode.PROXY,
            ),
            operations,
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["target"], "network")
        self.assertEqual(result["network_mode"], "proxy")
        self.assertTrue(result["recovery_observed"])
        self.assertIn(b"late harmless log", operations.published[0])
        self.assertIn(b"OpenSBI v1.7", operations.published[0])

    def test_web_gate_rejects_ready_marker_without_automatic_recovery(self) -> None:
        evidence = complete_web_network_evidence(NetworkMode.PROXY)
        operations = FakeOperations(boot=evidence, recovery=b"")

        result = run_gate(
            GateConfig(
                boot_timeout=1,
                drain_timeout=1,
                recovery_timeout=1,
                target=GateTarget.NETWORK,
                network_mode=NetworkMode.PROXY,
            ),
            operations,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "automatic recovery not observed")
        self.assertFalse(result["recovery_observed"])

    def test_web_gate_rejects_fatal_marker_before_recovery_epoch(self) -> None:
        evidence = complete_web_network_evidence(NetworkMode.PROXY)
        operations = FakeOperations(
            boot=evidence,
            recovery=(
                b"Kernel panic - not syncing\n"
                b"OpenSBI v1.7\nU-Boot 2025.01\n=> "
            ),
        )

        result = run_gate(
            GateConfig(
                boot_timeout=1,
                drain_timeout=1,
                recovery_timeout=1,
                target=GateTarget.NETWORK,
                network_mode=NetworkMode.PROXY,
            ),
            operations,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "fatal transcript marker: kernel panic")

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
        ):
            self.assertIn(
                f"systemd.setenv={variable}=/dev/ttyS0",
                bootargs.split(),
            )
        self.assertNotIn("ASTERINAS_BROWSER_M7_CONSOLE", bootargs)
        for variable, value in (
            ("ASTERINAS_DESKTOP_PROXY_URL", "http://10.100.19.216:17893"),
            ("ASTERINAS_DESKTOP_PROXY_HOST", "10.100.19.216"),
            ("ASTERINAS_DESKTOP_PROXY_PORT", "17893"),
            (
                "ASTERINAS_DESKTOP_FIXTURE_URL",
                "http://10.100.19.216:17894/asterinas-network-probe.bin",
            ),
            ("ASTERINAS_DESKTOP_FIXTURE_SIZE", "65536"),
            (
                "ASTERINAS_DESKTOP_FIXTURE_SHA256",
                "7daca2095d0438260fa849183dfc67faa459fdf4936e1bc91eec6b281b27e4c2",
            ),
            ("ASTERINAS_DESKTOP_FIXTURE_REQUESTS", "20"),
        ):
            self.assertIn(f"systemd.setenv={variable}={value}", bootargs.split())
        self.assertNotIn("saveenv", bootargs)
        self.assertNotIn("reboot_after", bootargs)
        recovery_bootargs = physical_bootargs(180)
        self.assertIn("asterinas.reboot_after=180", recovery_bootargs.split())
        self.assertNotIn("ASTERINAS_SAFE_REBOOT_AFTER", recovery_bootargs)
        self.assertNotIn("ASTERINAS_SAFE_REBOOT_CONSOLE", recovery_bootargs)
        self.assertNotIn("saveenv", recovery_bootargs)
        self.assertLess(
            len(f'setenv bootargs "{recovery_bootargs}"'.encode()),
            1024,
        )

        network_bootargs = physical_bootargs(
            180,
            target=GateTarget.NETWORK,
            network_mode=NetworkMode.PROXY,
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_M5_CONSOLE=/dev/ttyS0",
            network_bootargs.split(),
        )
        for unused_variable in (
            "ASTERINAS_DESKTOP_M4_CONSOLE",
            "ASTERINAS_BROWSER_M6_CONSOLE",
            "ASTERINAS_BROWSER_M7_CONSOLE",
        ):
            self.assertNotIn(unused_variable, network_bootargs)
        self.assertLess(
            len(f'setenv bootargs "{network_bootargs}"'.encode()),
            1024,
        )

        desktop_bootargs = physical_bootargs(180, target=GateTarget.DESKTOP)
        self.assertIn("asterinas.mmc_write_partition2", desktop_bootargs.split())
        self.assertIn("asterinas.reboot_after=180", desktop_bootargs.split())
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_M4_CONSOLE=/dev/ttyS0",
            desktop_bootargs.split(),
        )
        self.assertIn(
            "systemd.setenv=ASTERINAS_DESKTOP_BROWSER_ENABLED=0",
            desktop_bootargs.split(),
        )
        for service in (
            "asterinas-desktop-m5-network.service",
            "asterinas-desktop-m4-evidence.service",
            "asterinas-desktop-m6-browser.service",
            "asterinas-desktop-m7-baidu.service",
            "asterinas-desktop-m8-browser-quality.service",
        ):
            self.assertIn(f"systemd.mask={service}", desktop_bootargs.split())
        for excluded in (
            "asterinas.net=",
            "asterinas.neighbor=",
            "ASTERINAS_DESKTOP_M5_CONSOLE",
            "ASTERINAS_BROWSER_M6_CONSOLE",
            "ASTERINAS_DESKTOP_PROXY_",
            "ASTERINAS_DESKTOP_FIXTURE_",
        ):
            self.assertNotIn(excluded, desktop_bootargs)

    def test_physical_operations_owns_and_publishes_fixture_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            output.mkdir()
            fixture = FixtureServer(FixtureConfig("127.0.0.1", 0))
            arguments = argparse.Namespace(output_directory=output)
            operations = PhysicalGateOperations(arguments, fixture=fixture)

            with operations:
                self.assertTrue(fixture.running)
                operations.invalidate()
                result: dict[str, object] = {
                    "passed": True,
                    "reason": "pass",
                    "target": "network",
                }
                operations.publish(b"serial\n", result)
                self.assertIn("network_fixture", result)
                self.assertFalse(result["passed"])
                self.assertEqual(result["reason"], "network fixture evidence mismatch")
            self.assertFalse(fixture.running)

            summary = json.loads(
                (output / "network-fixture.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["request_count"], 0)
            self.assertEqual(summary["payload_size"], 65536)
            published = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(published["network_fixture"]["request_count"], 0)

    def test_firefox_publish_requires_and_retains_valid_png_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            output.mkdir()
            fixture = mock.Mock()
            fixture.summary.return_value = {
                "schema_version": 1,
                "payload_path": FIXTURE_PATH,
                "payload_sha256": PAYLOAD_SHA256,
                "payload_size": PAYLOAD_SIZE,
                "request_count": 20,
                "records_truncated": False,
                "requests": [
                    {
                        "body_bytes": PAYLOAD_SIZE,
                        "path": FIXTURE_PATH,
                        "status": 200,
                    }
                    for _ in range(20)
                ],
            }
            fixture.capture_payload.return_value = BROWSER_IMAGE
            fixture.capture_summary.return_value = {
                "bytes": len(BROWSER_IMAGE),
                "path": BROWSER_PNG_CAPTURE_PATH,
                "peer": BOARD_ADDRESS,
                "sha256": hashlib.sha256(BROWSER_IMAGE).hexdigest(),
            }
            arguments = argparse.Namespace(
                output_directory=output,
                target=GateTarget.FIREFOX,
                network_mode=NetworkMode.DIRECT,
                expected_crc32=None,
            )
            operations = PhysicalGateOperations(arguments, fixture=fixture)
            try:
                operations.invalidate()
                result: dict[str, object] = {
                    "passed": True,
                    "reason": "pass",
                    "target": "firefox",
                }
                operations.publish(b"serial\n", result)
            finally:
                operations.output.close()

            self.assertTrue(result["passed"])
            self.assertEqual((output / "baidu-search.png").read_bytes(), BROWSER_IMAGE)
            self.assertEqual(
                result["baidu_screenshot"]["path"], BROWSER_PNG_CAPTURE_PATH
            )
            self.assertEqual(
                result["baidu_screenshot"]["artifact"], "baidu-search.png"
            )

    def test_proxy_mode_owns_bridge_and_publishes_its_summary(self) -> None:
        self.assertIn(
            "proxy_bridge",
            inspect.signature(PhysicalGateOperations).parameters,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            output.mkdir()
            fixture = FixtureServer(FixtureConfig("127.0.0.1", 0))
            proxy = FakeProxyBridge()
            arguments = argparse.Namespace(
                output_directory=output,
                target=GateTarget.NETWORK,
                network_mode=NetworkMode.PROXY,
                expected_crc32={
                    "booti": "12345678",
                    "dtb": "90abcdef",
                    "initrd": "deadbeef",
                },
            )
            operations = PhysicalGateOperations(
                arguments,
                fixture=fixture,
                proxy_bridge=proxy,
            )

            with operations:
                self.assertTrue(fixture.running)
                self.assertTrue(proxy.running)
                operations.invalidate()
                result: dict[str, object] = {
                    "passed": False,
                    "reason": "test",
                    "target": "network",
                    "network_mode": "proxy",
                }
                operations.publish(b"serial\n", result)

            self.assertFalse(fixture.running)
            self.assertFalse(proxy.running)
            self.assertEqual(proxy.events, ["start", "close"])
            published = json.loads((output / "result.json").read_text())
            self.assertEqual(
                published["proxy_bridge"]["upstream"],
                "127.0.0.1:17892",
            )
            self.assertEqual(
                published["artifact_crc32"],
                {
                    "booti": "12345678",
                    "dtb": "90abcdef",
                    "initrd": "deadbeef",
                },
            )
            self.assertEqual(
                json.loads((output / "proxy-bridge.json").read_text())["listen"],
                "10.100.19.216:17893",
            )

    def test_proxy_bridge_start_failure_closes_started_fixture(self) -> None:
        self.assertIn(
            "proxy_bridge",
            inspect.signature(PhysicalGateOperations).parameters,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            output.mkdir()
            fixture = FixtureServer(FixtureConfig("127.0.0.1", 0))
            proxy = FakeProxyBridge(fail_start=True)
            arguments = argparse.Namespace(
                output_directory=output,
                target=GateTarget.NETWORK,
                network_mode=NetworkMode.PROXY,
            )
            operations = PhysicalGateOperations(
                arguments,
                fixture=fixture,
                proxy_bridge=proxy,
            )

            with self.assertRaisesRegex(RuntimeError, "bridge start failed"):
                with operations:
                    self.fail("unreachable")

            self.assertFalse(fixture.running)
            self.assertEqual(proxy.events, ["start", "close"])

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
        self.assertEqual(parsed.target, GateTarget.BROWSER)

        network = _parse_args(
            required[:-2]
            + [
                "--target",
                "network",
                "--network-mode",
                "proxy",
                "--reboot-after",
                "300",
            ]
        )
        self.assertEqual(network.target, GateTarget.NETWORK)
        self.assertEqual(network.network_mode, NetworkMode.PROXY)
        desktop = _parse_args(required[:-2] + ["--target", "desktop"])
        self.assertEqual(desktop.target, GateTarget.DESKTOP)

    def test_address_conflict_is_rejected_before_serial_open(self) -> None:
        operations = FakeOperations(conflict=True)

        result = run_gate(GateConfig(boot_timeout=1, drain_timeout=1), operations)

        self.assertFalse(result["passed"])
        self.assertEqual(
            operations.events,
            ["invalidate", "address-check", "publish"],
        )
        self.assertIsNotNone(operations.published)
        self.assertEqual(result["target"], "browser")

    def test_network_target_passes_without_desktop_or_browser_evidence(self) -> None:
        evidence = complete_web_network_evidence(NetworkMode.PROXY)
        operations = FakeOperations(
            chunks=(evidence[13:-17], evidence[-17:], b"late harmless log\n"),
            boot=evidence[:13],
        )

        result = run_gate(
            GateConfig(
                boot_timeout=1,
                drain_timeout=1,
                target=GateTarget.NETWORK,
                network_mode=NetworkMode.PROXY,
            ),
            operations,
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["target"], "network")
        self.assertNotIn("javascript_status", result)
        self.assertNotIn(DESKTOP_M4_MILESTONES[-1].encode(), operations.published[0])
        self.assertNotIn(DESKTOP_M6_REMOTE_MARKER.encode(), operations.published[0])
        self.assertIn(b"late harmless log", operations.published[0])

    def test_desktop_target_passes_without_network_or_browser_evidence(self) -> None:
        evidence = complete_desktop_evidence()
        operations = FakeOperations(
            chunks=(
                b"DEBIAN_NETWORK_M5_FAIL reason=megrez-bootarg\n",
                b"DEBIAN_BROWSER_M6_FAIL reason=browser-start-timeout\n"
                + evidence,
                b"late harmless log\n",
            ),
        )

        result = run_gate(
            GateConfig(
                boot_timeout=1,
                drain_timeout=1,
                target=GateTarget.DESKTOP,
            ),
            operations,
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["target"], "desktop")
        self.assertNotIn("javascript_status", result)
        self.assertEqual(PHYSICAL_DESKTOP_MILESTONES[-1], evidence.splitlines()[-1])
        self.assertNotIn("address-check", operations.events)
        self.assertIn(b"late harmless log", operations.published[0])
        self.assertTrue(classify_physical_desktop_transcript(evidence).passed)
        self.assertTrue(
            classify_physical_desktop_transcript(
                b"DEBIAN_NETWORK_M5_FAIL reason=megrez-bootarg\n"
                b"DEBIAN_BROWSER_M7_FAIL reason=process-search\n" + evidence
            ).passed
        )

    def test_network_classifier_rejects_bad_or_fatal_evidence(self) -> None:
        complete = complete_network_evidence()
        self.assertTrue(classify_physical_network_transcript(complete).passed)
        cases = (
            (
                complete.replace(EXPECTED_PHYSICAL_NETWORK_MILESTONES[-1], b""),
                "missing physical network milestone",
            ),
            (
                complete + EXPECTED_PHYSICAL_NETWORK_MILESTONES[-1] + b"\n",
                "duplicate physical network milestone",
            ),
            (
                b"\n".join(reversed(EXPECTED_PHYSICAL_NETWORK_MILESTONES)),
                "physical network milestones out of order",
            ),
            (
                complete + b"Kernel panic - not syncing\n",
                "fatal transcript marker: kernel panic",
            ),
        )
        for transcript, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    classify_physical_network_transcript(transcript).reason,
                    reason,
                )

    def test_network_target_rejects_fatal_marker_seen_during_drain(self) -> None:
        operations = FakeOperations(
            chunks=(complete_network_evidence(), b"Fatal bus error\n"),
        )

        result = run_gate(
            GateConfig(
                boot_timeout=1,
                drain_timeout=1,
                target=GateTarget.NETWORK,
                network_mode=NetworkMode.PROXY,
            ),
            operations,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["target"], "network")
        self.assertEqual(
            result["reason"], "fatal transcript marker: GMAC fatal bus error"
        )

    def test_split_browser_network_markers_pass_without_icmp(self) -> None:
        self.assertEqual(PHYSICAL_MILESTONES, EXPECTED_PHYSICAL_MILESTONES)
        self.assertEqual(PHYSICAL_M7_MILESTONES, EXPECTED_PHYSICAL_M7_MILESTONES)
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
        self.assertEqual(result["target"], "browser")
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
                + b"\nDEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass\n"
                + b"\n".join(EXPECTED_PHYSICAL_M7_MILESTONES),
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
            (
                complete + b"DEBIAN_BROWSER_M7_FAIL reason=home-title-timeout\n",
                "fatal transcript marker: Baidu page guest failure",
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
