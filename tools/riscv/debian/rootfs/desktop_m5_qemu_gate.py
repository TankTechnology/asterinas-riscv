#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Prove Debian M5 HTTPS and application desktop behavior through QEMU slirp."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_MILESTONES,
    DesktopM4Operations,
    desktop_m4_qemu_argv,
)
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
    NetworkMode,
    classify_desktop_m5_qemu,
    classify_web_network,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import (
    GateTermination,
    TerminationSignalState,
)
from tools.riscv.debian.rootfs.rootfs_gate import (
    GateConfig,
    GateFailure,
    parse_gate_args,
)
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate
from tools.riscv.megrez_network_fixture import (
    FIXTURE_PATH,
    PAYLOAD_SHA256,
    PAYLOAD_SIZE,
    FixtureConfig,
    FixtureServer,
    is_successful_summary,
)
from tools.riscv.megrez_proxy_bridge import (
    ProxyBridge,
    proxy_bridge_config_from_environment,
)


QEMU_FIXTURE_PORT = 17894
QEMU_FIXTURE_REQUESTS = 20
QEMU_NETWORK_TIMEOUT_SECONDS = 120
QEMU_FIXTURE_URL = f"http://10.0.2.2:{QEMU_FIXTURE_PORT}{FIXTURE_PATH}"
QEMU_PROXY_URL = "http://10.0.2.2:17893"
DESKTOP_M5_QEMU_BOOTARGS = (
    "console=ttyS0 loglevel=4 init=/init "
    "asterinas.debian_network=qemu-slirp "
    f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_URL={QEMU_FIXTURE_URL} "
    f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_SIZE={PAYLOAD_SIZE} "
    f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_SHA256={PAYLOAD_SHA256} "
    f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_REQUESTS={QEMU_FIXTURE_REQUESTS} "
    f"systemd.setenv=ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS={QEMU_NETWORK_TIMEOUT_SECONDS} "
    "-- --root-init=systemd"
)


class QemuGateTarget(str, Enum):
    """Select network-only or complete browser-desktop evidence."""

    NETWORK = "network"
    BROWSER = "browser"


class QemuExpectedFailure(str, Enum):
    """A negative path whose exact guest failure is considered success."""

    NONE = "none"
    PROXY_UNAVAILABLE = "proxy-unavailable"
    DNS = "dns"
    TLS = "tls"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class QemuGateSelection:
    """Mode options parsed before the common rootfs gate arguments."""

    target: QemuGateTarget
    network_mode: NetworkMode | None
    expected_failure: QemuExpectedFailure


class QemuTlsFixture:
    """Own the repository's self-signed TLS endpoint for one negative gate."""

    def __init__(self) -> None:
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._log: object | None = None
        self._pid: int | None = None
        self._exit_status: int | None = None
        self._ready = False

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        repository = Path(__file__).resolve().parents[4]
        generator = repository / "tools/riscv/xorg/gen_tls_certs.py"
        server = repository / "tools/riscv/xorg/tls_cert_server.py"
        temporary = tempfile.TemporaryDirectory(prefix="asterinas-qemu-tls-")
        self._temporary = temporary
        root = Path(temporary.name)
        certs = root / "certs"
        try:
            generated = subprocess.run(
                (sys.executable, str(generator), str(certs)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=30,
            )
            if generated.returncode != 0:
                raise GateFailure("TLS fixture certificate generation failed")
            log = (root / "tls-server.log").open("w+b")
            self._log = log
            process = subprocess.Popen(
                (sys.executable, str(server), str(certs)),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            self._process = process
            self._pid = process.pid
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                status = process.poll()
                if status is not None:
                    self._exit_status = status
                    raise GateFailure(f"TLS fixture exited with status {status}")
                try:
                    connection = socket.create_connection(
                        ("127.0.0.1", 8446), timeout=0.5
                    )
                except OSError:
                    time.sleep(0.05)
                    continue
                connection.close()
                self._ready = True
                return
            raise GateFailure("TLS fixture startup deadline expired")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self._process
        self._process = None
        self._ready = False
        if process is not None:
            status = process.poll()
            if status is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    status = process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    status = process.wait(timeout=2.0)
            self._exit_status = status
        log = self._log
        self._log = None
        if log is not None:
            log.close()
        temporary = self._temporary
        self._temporary = None
        if temporary is not None:
            temporary.cleanup()

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "endpoint": "https://10.0.2.2:8446/",
            "certificate": "selfsigned",
            "pid": self._pid,
            "ready": self._ready,
            "exit_status": self._exit_status,
        }


def qemu_web_network_bootargs(
    mode: NetworkMode,
    *,
    expected_failure: QemuExpectedFailure = QemuExpectedFailure.NONE,
) -> str:
    """Return an isolated QEMU web-network environment for one selected mode."""

    if not isinstance(mode, NetworkMode):
        raise ValueError("mode must be a NetworkMode")
    if not isinstance(expected_failure, QemuExpectedFailure):
        raise ValueError("expected failure must be a QemuExpectedFailure")
    resolver = "192.0.2.1" if expected_failure is QemuExpectedFailure.DNS else "10.0.2.3"
    mode_arguments = (
        f"systemd.setenv=ASTERINAS_WEB_NETWORK_MODE={mode.value} "
        "systemd.setenv=ASTERINAS_WEB_NETWORK_ADDRESS=10.0.2.15/24 "
        "systemd.setenv=ASTERINAS_WEB_NETWORK_GATEWAY=10.0.2.2 "
    )
    if mode is NetworkMode.PROXY:
        mode_arguments += (
            f"systemd.setenv=ASTERINAS_DESKTOP_PROXY_URL={QEMU_PROXY_URL} "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=10.0.2.2 "
            "systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=17893 "
        )
    else:
        mode_arguments += (
            f"systemd.setenv=ASTERINAS_WEB_NETWORK_RESOLVER={resolver} "
        )
    if expected_failure is QemuExpectedFailure.TLS:
        mode_arguments += (
            "systemd.setenv=ASTERINAS_WEB_NETWORK_HTTPS_URL="
            "https://10.0.2.2:8446/ "
        )
    return (
        "console=ttyS0 loglevel=4 init=/init "
        "asterinas.debian_network=qemu-slirp "
        f"{mode_arguments}"
        f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_URL={QEMU_FIXTURE_URL} "
        f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_SIZE={PAYLOAD_SIZE} "
        f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_SHA256={PAYLOAD_SHA256} "
        f"systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_REQUESTS={QEMU_FIXTURE_REQUESTS} "
        f"systemd.setenv=ASTERINAS_DESKTOP_M5_TIMEOUT_SECONDS={QEMU_NETWORK_TIMEOUT_SECONDS} "
        "-- --root-init=systemd"
    )


def classify_qemu_web_network(
    transcript: bytes,
    *,
    mode: NetworkMode,
    expected_failure: QemuExpectedFailure,
) -> GateResult:
    """Classify a positive web path or one exact negative-path result."""

    if expected_failure is QemuExpectedFailure.NONE:
        return classify_web_network(transcript, mode=mode)
    expected = {
        QemuExpectedFailure.PROXY_UNAVAILABLE: (
            NetworkMode.PROXY,
            "https",
            "proxy-unavailable",
        ),
        QemuExpectedFailure.DNS: (NetworkMode.DIRECT, "dns", "resolve"),
        QemuExpectedFailure.TLS: (NetworkMode.DIRECT, "https", "tls"),
    }[expected_failure]
    expected_mode, layer, reason = expected
    if mode is not expected_mode:
        return GateResult(False, "expected failure is invalid for network mode", None)
    marker = (
        f"DEBIAN_WEB_NETWORK_FAIL mode={mode.value} layer={layer} reason={reason}"
    ).encode()
    if transcript.count(marker) != 1:
        return GateResult(False, "missing or duplicate expected web failure", None)
    if transcript.count(b"DEBIAN_WEB_NETWORK_FAIL mode=") != 1:
        return GateResult(False, "unexpected additional web failure", None)
    if b"DEBIAN_WEB_NETWORK_READY mode=" in transcript:
        return GateResult(False, "ready marker present in negative web gate", None)
    return GateResult(True, "pass", None)


def _parse_target(
    arguments: list[str] | None,
) -> tuple[QemuGateSelection, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--target",
        choices=tuple(target.value for target in QemuGateTarget),
        default=QemuGateTarget.BROWSER.value,
    )
    parser.add_argument("--network-mode", choices=tuple(NetworkMode), type=NetworkMode)
    parser.add_argument(
        "--expect-failure",
        choices=tuple(QemuExpectedFailure),
        type=QemuExpectedFailure,
        default=QemuExpectedFailure.NONE,
    )
    values, remaining = parser.parse_known_args(
        sys.argv[1:] if arguments is None else arguments
    )
    target = QemuGateTarget(values.target)
    if target is QemuGateTarget.NETWORK:
        if values.network_mode is None:
            parser.error("network target requires --network-mode")
    elif values.network_mode is not None or values.expect_failure is not QemuExpectedFailure.NONE:
        parser.error("web network options require --target network")
    if values.expect_failure is QemuExpectedFailure.PROXY_UNAVAILABLE:
        if values.network_mode is not NetworkMode.PROXY:
            parser.error("proxy-unavailable requires proxy mode")
    if values.expect_failure in (QemuExpectedFailure.DNS, QemuExpectedFailure.TLS):
        if values.network_mode is not NetworkMode.DIRECT:
            parser.error("dns and tls failures require direct mode")
    return (
        QemuGateSelection(target, values.network_mode, values.expect_failure),
        remaining,
    )


def desktop_m5_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Add exactly one slirp-backed VirtIO NIC to the M4 device contract."""

    base = desktop_m4_qemu_argv(**arguments)
    nic_index = base.index("-nic")
    if base[nic_index : nic_index + 2] != ("-nic", "none"):
        raise ValueError("unexpected M4 QEMU NIC contract")
    return (
        *base[:nic_index],
        "-netdev",
        "user,id=net0",
        "-device",
        "virtio-net-device,netdev=net0",
        *base[nic_index + 2 :],
    )


class DesktopM5QemuOperations(DesktopM4Operations):
    """M5 identity and slirp networking over the bounded M4 lifecycle."""

    SCHEMA_VERSION = 5
    PROFILE_NAME = "desktop-m5-network"
    ARTIFACT_PREFIX = "desktop-m5-qemu"
    MILESTONES = (*DESKTOP_M5_QEMU_MILESTONES, *DESKTOP_M4_MILESTONES)
    FAILURE_MARKER = b"DEBIAN_DESKTOP_M4_FAIL reason="
    BOOTARGS = DESKTOP_M5_QEMU_BOOTARGS
    TARGET = QemuGateTarget.BROWSER

    def __init__(
        self,
        config: GateConfig,
        *,
        fixture: FixtureServer | None = None,
    ) -> None:
        self.fixture = fixture or FixtureServer(
            FixtureConfig("127.0.0.1", QEMU_FIXTURE_PORT)
        )
        super().__init__(config)

    def __enter__(self) -> DesktopM5QemuOperations:
        try:
            self.fixture.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            self.fixture.close()
        finally:
            super().close()

    def invalidate(self, config: GateConfig) -> None:
        super().invalidate(config)
        self._require_output().invalidate("network-fixture.json")

    def _fixture_evidence_passes(self, summary: dict[str, object]) -> bool:
        return is_successful_summary(
            summary, expected_requests=QEMU_FIXTURE_REQUESTS
        )

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        summary = self.fixture.summary()
        result["target"] = self.TARGET.value
        result["network_fixture"] = summary
        if result.get("passed") is True and not self._fixture_evidence_passes(summary):
            result["passed"] = False
            result["reason"] = "network fixture evidence mismatch"
        fixture_payload = (
            json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        self._require_output().atomic_write("network-fixture.json", fixture_payload)
        super().publish(config, prepared, transcript, result)

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_m5_qemu_argv(**arguments)


class NetworkM5QemuOperations(DesktopM5QemuOperations):
    """Stop after network evidence without waiting for desktop readiness."""

    ARTIFACT_PREFIX = "desktop-m5-network-qemu"
    MILESTONES = DESKTOP_M5_QEMU_MILESTONES
    FAILURE_MARKER = b"DEBIAN_NETWORK_M5_FAIL reason="
    ADDITIONAL_FAILURE_MARKERS = ()
    CAPTURE_SCREENSHOT = False
    TARGET = QemuGateTarget.NETWORK

    @classmethod
    def _accepted_profile_identities(cls) -> tuple[tuple[int, str], ...]:
        return (
            (cls.SCHEMA_VERSION, cls.PROFILE_NAME),
            (7, "browser-web"),
        )

    def __init__(
        self,
        config: GateConfig,
        *,
        network_mode: NetworkMode,
        expected_failure: QemuExpectedFailure = QemuExpectedFailure.NONE,
        fixture: FixtureServer | None = None,
        proxy_bridge: ProxyBridge | None = None,
        tls_fixture: QemuTlsFixture | None = None,
    ) -> None:
        self.network_mode = network_mode
        self.expected_failure = expected_failure
        self.BOOTARGS = qemu_web_network_bootargs(
            network_mode,
            expected_failure=expected_failure,
        )
        if expected_failure is QemuExpectedFailure.NONE:
            self.MILESTONES = (
                f"DEBIAN_WEB_NETWORK_READY mode={network_mode.value} layers=10",
            )
            self.FAILURE_MARKER = b"DEBIAN_WEB_NETWORK_FAIL mode="
        else:
            expected = {
                QemuExpectedFailure.PROXY_UNAVAILABLE: (
                    "https",
                    "proxy-unavailable",
                ),
                QemuExpectedFailure.DNS: ("dns", "resolve"),
                QemuExpectedFailure.TLS: ("https", "tls"),
            }[expected_failure]
            layer, reason = expected
            self.MILESTONES = (
                f"DEBIAN_WEB_NETWORK_FAIL mode={network_mode.value} "
                f"layer={layer} reason={reason}",
            )
            self.FAILURE_MARKER = b"DEBIAN_WEB_NETWORK_FAIL mode="
        should_start_proxy = (
            network_mode is NetworkMode.PROXY
            and expected_failure is not QemuExpectedFailure.PROXY_UNAVAILABLE
        )
        self.proxy_bridge = proxy_bridge
        if should_start_proxy and self.proxy_bridge is None:
            self.proxy_bridge = ProxyBridge(
                proxy_bridge_config_from_environment(listen_address="0.0.0.0")
            )
        self._start_proxy = should_start_proxy
        self.tls_fixture = tls_fixture
        if expected_failure is QemuExpectedFailure.TLS and self.tls_fixture is None:
            self.tls_fixture = QemuTlsFixture()
        super().__init__(config, fixture=fixture)

    def _completion_is_failure(
        self, completion: bytes, failure_markers: tuple[bytes, ...]
    ) -> bool:
        if (
            self.expected_failure is not QemuExpectedFailure.NONE
            and completion == self.MILESTONES[-1].encode()
        ):
            return False
        return super()._completion_is_failure(completion, failure_markers)

    def _fixture_evidence_passes(self, summary: dict[str, object]) -> bool:
        expected_requests = {
            QemuExpectedFailure.NONE: QEMU_FIXTURE_REQUESTS,
            QemuExpectedFailure.PROXY_UNAVAILABLE: 1,
            QemuExpectedFailure.DNS: 0,
            QemuExpectedFailure.TLS: 1,
        }[self.expected_failure]
        if expected_requests:
            return is_successful_summary(
                summary, expected_requests=expected_requests
            )
        return (
            summary.get("schema_version") == 1
            and summary.get("payload_path") == FIXTURE_PATH
            and summary.get("payload_sha256") == PAYLOAD_SHA256
            and summary.get("payload_size") == PAYLOAD_SIZE
            and summary.get("request_count") == 0
            and summary.get("requests") == []
            and summary.get("records_truncated") is False
        )

    def __enter__(self) -> NetworkM5QemuOperations:
        try:
            super().__enter__()
            if self.tls_fixture is not None:
                self.tls_fixture.start()
            if self._start_proxy:
                assert self.proxy_bridge is not None
                self.proxy_bridge.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.proxy_bridge is not None:
                self.proxy_bridge.close()
        finally:
            try:
                if self.tls_fixture is not None:
                    self.tls_fixture.close()
            finally:
                super().close()

    def invalidate(self, config: GateConfig) -> None:
        super().invalidate(config)
        self._require_output().invalidate("proxy-bridge.json", "tls-fixture.json")

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        result["network_mode"] = self.network_mode.value
        result["expected_failure"] = self.expected_failure.value
        if self.expected_failure is not QemuExpectedFailure.NONE:
            classified = classify_qemu_web_network(
                transcript,
                mode=self.network_mode,
                expected_failure=self.expected_failure,
            )
            if classified.passed:
                marker = self.MILESTONES[-1]
                match = marker.split(" layer=", 1)[1]
                layer, reason = match.split(" reason=", 1)
                result["classified_guest_failure"] = {
                    "mode": self.network_mode.value,
                    "layer": layer,
                    "reason": reason,
                }
        if self.proxy_bridge is not None:
            summary = self.proxy_bridge.summary()
            result["proxy_bridge"] = summary
            payload = (
                json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            self._require_output().atomic_write("proxy-bridge.json", payload)
        if self.tls_fixture is not None:
            summary = self.tls_fixture.summary()
            result["tls_fixture"] = summary
            payload = (
                json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            self._require_output().atomic_write("tls-fixture.json", payload)
        super().publish(config, prepared, transcript, result)


def orchestrate_desktop_m5_qemu_gate(
    config: GateConfig, operations: DesktopM5QemuOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m5_qemu,
    )


def orchestrate_network_m5_qemu_gate(
    config: GateConfig, operations: NetworkM5QemuOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=lambda transcript, expected_debian_release: (
            classify_qemu_web_network(
                transcript,
                mode=operations.network_mode,
                expected_failure=operations.expected_failure,
            )
        ),
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        selection, gate_arguments = _parse_target(arguments)
        config = parse_gate_args(gate_arguments)
        _safe_output(config.output_directory)
        operations_type = (
            NetworkM5QemuOperations
            if selection.target is QemuGateTarget.NETWORK
            else DesktopM5QemuOperations
        )
        operation_arguments: dict[str, object] = {}
        if selection.target is QemuGateTarget.NETWORK:
            assert selection.network_mode is not None
            operation_arguments = {
                "network_mode": selection.network_mode,
                "expected_failure": selection.expected_failure,
            }
        with TerminationSignalState(), operations_type(
            config, **operation_arguments
        ) as operations:
            if selection.target is QemuGateTarget.NETWORK:
                result = orchestrate_network_m5_qemu_gate(config, operations)
            else:
                result = orchestrate_desktop_m5_qemu_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m5-qemu-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m5-qemu-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
