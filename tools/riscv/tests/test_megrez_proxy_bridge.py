#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Lifecycle tests for the owned Megrez-to-Clash proxy bridge."""

from __future__ import annotations

import io
import importlib
import unittest
from pathlib import Path
from typing import Any
import signal

try:
    bridge: Any = importlib.import_module("tools.riscv.megrez_proxy_bridge")
except ModuleNotFoundError:
    bridge = None


class FakeProcess:
    def __init__(self, *, exit_status: int | None = None) -> None:
        self.pid = 4242
        self.exit_status = exit_status
        self.terminated = 0
        self.killed = 0
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.exit_status

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.exit_status is None:
            raise TimeoutError
        return self.exit_status


class FakeProcessFactory:
    def __init__(self, process: FakeProcess | None = None) -> None:
        self.process = process or FakeProcess()
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> FakeProcess:
        self.calls.append((argv, kwargs))
        return self.process


class EndpointState:
    def __init__(self, *, upstream: bool = True, listener_after: int = 2) -> None:
        self.upstream = upstream
        self.listener_after = listener_after
        self.listener_probes = 0

    def __call__(self, address: str, port: int, timeout: float) -> bool:
        self.assert_timeout(timeout)
        if (address, port) == ("127.0.0.1", 17892):
            return self.upstream
        if (address, port) == ("10.100.19.216", 17893):
            self.listener_probes += 1
            return self.listener_probes >= self.listener_after
        raise AssertionError(f"unexpected endpoint: {address}:{port}")

    @staticmethod
    def assert_timeout(timeout: float) -> None:
        if not 0 < timeout <= 1:
            raise AssertionError(f"unbounded probe timeout: {timeout}")


class ProxyBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(bridge, "Megrez proxy bridge module must exist")

    def test_starts_exact_socat_bridge_after_preflight(self) -> None:
        factory = FakeProcessFactory()
        endpoints = EndpointState()
        clock = iter((0.0, 0.0, 0.1, 0.2))
        instance = bridge.ProxyBridge(
            bridge.ProxyBridgeConfig(),
            process_factory=factory,
            endpoint_probe=endpoints,
            monotonic=lambda: next(clock),
            sleeper=lambda _seconds: None,
            stderr_file=io.BytesIO(),
            terminate_group=lambda _pid, _signal: None,
        )

        returned = instance.start()

        self.assertIs(returned, instance)
        self.assertTrue(instance.running)
        self.assertEqual(
            factory.calls[0][0],
            (
                "socat",
                "TCP-LISTEN:17893,bind=10.100.19.216,reuseaddr,fork",
                "TCP:127.0.0.1:17892",
            ),
        )
        self.assertIs(factory.calls[0][1]["stderr"], instance.stderr_file)
        self.assertTrue(factory.calls[0][1]["start_new_session"])
        self.assertEqual(
            instance.summary(),
            {
                "schema_version": 1,
                "listen": "10.100.19.216:17893",
                "upstream": "127.0.0.1:17892",
                "pid": 4242,
                "ready": True,
                "exit_status": None,
                "stderr_hex": "",
            },
        )

    def test_rejects_missing_upstream_and_occupied_listener_before_spawn(self) -> None:
        for name, endpoints, reason in (
            (
                "upstream",
                EndpointState(upstream=False),
                "proxy-upstream-unavailable",
            ),
            (
                "listener",
                EndpointState(listener_after=1),
                "proxy-listener-in-use",
            ),
        ):
            with self.subTest(name=name):
                factory = FakeProcessFactory()
                instance = bridge.ProxyBridge(
                    bridge.ProxyBridgeConfig(),
                    process_factory=factory,
                    endpoint_probe=endpoints,
                    monotonic=lambda: 0.0,
                    sleeper=lambda _seconds: None,
                    stderr_file=io.BytesIO(),
                    terminate_group=lambda _pid, _signal: None,
                )

                with self.assertRaisesRegex(bridge.ProxyBridgeError, f"^{reason}$"):
                    instance.start()
                self.assertEqual(factory.calls, [])
                self.assertFalse(instance.running)

    def test_early_exit_and_startup_timeout_are_bounded_and_cleaned(self) -> None:
        cases = (
            (FakeProcess(exit_status=23), 2, "proxy-bridge-exited:23"),
            (FakeProcess(), 100, "proxy-bridge-startup-timeout"),
        )
        for process, listener_after, reason in cases:
            with self.subTest(reason=reason):
                factory = FakeProcessFactory(process)
                endpoints = EndpointState(listener_after=listener_after)
                ticks = iter((0.0, 0.0, 0.1, 6.0, 6.0, 6.0))
                signals: list[tuple[int, int]] = []

                def terminate(pid: int, signum: int) -> None:
                    signals.append((pid, signum))
                    if signum == signal.SIGKILL:
                        process.exit_status = -signal.SIGKILL

                instance = bridge.ProxyBridge(
                    bridge.ProxyBridgeConfig(startup_timeout=5.0),
                    process_factory=factory,
                    endpoint_probe=endpoints,
                    monotonic=lambda: next(ticks),
                    sleeper=lambda _seconds: None,
                    stderr_file=io.BytesIO(b"bounded failure\n"),
                    terminate_group=terminate,
                )

                with self.assertRaisesRegex(bridge.ProxyBridgeError, f"^{reason}$"):
                    instance.start()
                self.assertFalse(instance.running)
                if process.exit_status is None:
                    self.assertTrue(signals)

    def test_context_failure_and_repeated_close_reap_only_owned_group(self) -> None:
        process = FakeProcess()
        factory = FakeProcessFactory(process)
        endpoints = EndpointState()
        clock = iter((0.0, 0.0, 0.1, 0.2))
        signals: list[tuple[int, int]] = []
        instance = bridge.ProxyBridge(
            bridge.ProxyBridgeConfig(),
            process_factory=factory,
            endpoint_probe=endpoints,
            monotonic=lambda: next(clock),
            sleeper=lambda _seconds: None,
            stderr_file=io.BytesIO(),
            terminate_group=lambda pid, signal: (
                signals.append((pid, signal)),
                setattr(process, "exit_status", 0),
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with instance:
                raise RuntimeError("body failed")
        instance.close()

        self.assertFalse(instance.running)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0][0], 4242)
        self.assertEqual(process.wait_timeouts, [2.0])

    def test_configuration_is_canonical_and_bounded(self) -> None:
        invalid = (
            {"listen_address": "localhost"},
            {"listen_address": "127.0.0.01"},
            {"listen_port": 0},
            {"listen_port": True},
            {"upstream_address": "::1"},
            {"upstream_port": 65536},
            {"startup_timeout": 0},
            {"startup_timeout": float("inf")},
            {"shutdown_timeout": float("nan")},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    bridge.ProxyBridgeConfig(**values)

    def test_megrez_gmac_unit_target_runs_bridge_lifecycle_tests(self) -> None:
        makefile = Path(__file__).resolve().parents[3] / "Makefile"
        target = (
            makefile.read_text(encoding="utf-8")
            .split(".PHONY: test_riscv_megrez_gmac_unit", 1)[1]
            .split(".PHONY:", 1)[0]
        )

        self.assertIn("tools.riscv.tests.test_megrez_proxy_bridge", target)


if __name__ == "__main__":
    unittest.main()
