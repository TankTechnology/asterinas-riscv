#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the independent one-datagram network gate under QEMU."""

from __future__ import annotations

import json
import secrets
import threading
from typing import Any, Mapping

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DesktopM4Operations,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import desktop_m5_qemu_argv
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate
from tools.riscv.megrez_single_packet_fixture import OneShotUdpFixture, expected_summary
from tools.riscv.megrez_single_packet_gate import parse_guest_receipt, validate_packet_exchange


QEMU_SINGLE_PACKET_PORT = 17895
QEMU_SINGLE_PACKET_PEER = "10.0.2.2"


def single_packet_qemu_bootargs(nonce: str) -> str:
    """Build the bounded QEMU boot contract for one UDP application packet."""

    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise ValueError("nonce must be 32 lowercase hexadecimal digits")
    return (
        "console=ttyS0 loglevel=4 init=/init "
        "asterinas.debian_network=qemu-slirp "
        f"asterinas.single_packet_nonce={nonce} "
        f"asterinas.single_packet_peer={QEMU_SINGLE_PACKET_PEER} "
        f"asterinas.single_packet_port={QEMU_SINGLE_PACKET_PORT} "
        "systemd.unit=asterinas-single-packet.service "
        "systemd.setenv=ASTERINAS_DESKTOP_M5_COMMAND_TIMEOUT_SECONDS=30 "
        "-- --root-init=systemd"
    )


def classify_single_packet_qemu(
    transcript: bytes, *, nonce: str
) -> GateResult:
    """Require one successful guest marker and no failed marker."""

    if not isinstance(transcript, bytes):
        return GateResult(False, "QEMU transcript must be bytes", None)
    text = transcript.decode("ascii", errors="replace")
    try:
        receipt = parse_guest_receipt(text, nonce)
    except ValueError as error:
        return GateResult(False, str(error), None)
    if (receipt.tx, receipt.rx, receipt.tx_errors, receipt.rx_errors) != (1, 1, 0, 0):
        return GateResult(False, "guest did not report one error-free exchange", None)
    if receipt.payload_sha256 != expected_summary(nonce).payload_sha256:
        return GateResult(False, "guest payload hash mismatch", None)
    if any(marker in transcript.lower() for marker in (b"kernel panic", b"status=fail", b"segmentation fault")):
        return GateResult(False, "guest reported a failure", None)
    return GateResult(True, "pass", None)


class SinglePacketQemuOperations(DesktopM4Operations):
    """Reuse the bounded framebuffer boot, replacing M5 HTTP with one UDP."""

    SCHEMA_VERSION = 5
    PROFILE_NAME = "desktop-m5-network"
    ARTIFACT_PREFIX = "megrez-single-packet-qemu"
    CAPTURE_SCREENSHOT = False

    def __init__(self, config: GateConfig) -> None:
        self.nonce = secrets.token_hex(16)
        self.BOOTARGS = single_packet_qemu_bootargs(self.nonce)
        self.MILESTONES = (
            "ASTERINAS_GMAC_SINGLE_PACKET "
            f"nonce={self.nonce} tx=1 rx=1 tx_errors=0 rx_errors=0 "
            f"payload_sha256={expected_summary(self.nonce).payload_sha256} status=pass",
        )
        self.FAILURE_MARKER = b"status=fail"
        self._fixture = OneShotUdpFixture("127.0.0.1", QEMU_SINGLE_PACKET_PORT)
        self._fixture_thread: threading.Thread | None = None
        self._fixture_error: BaseException | None = None
        self._fixture_summary: Mapping[str, Any] | None = None
        super().__init__(config)

    @classmethod
    def _accepted_profile_identities(cls) -> tuple[tuple[int, str], ...]:
        return ((cls.SCHEMA_VERSION, cls.PROFILE_NAME),)

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_m5_qemu_argv(**arguments)

    def __enter__(self) -> "SinglePacketQemuOperations":
        super().__enter__()
        self._fixture.__enter__()
        return self

    def launch(self, config: GateConfig, prepared: Any) -> dict[str, Any]:

        def receive() -> None:
            try:
                self._fixture_summary = self._fixture.receive_once(
                    self.nonce, config.boot_timeout + config.command_timeout
                ).as_dict()
            except BaseException as error:  # published as a failed receipt
                self._fixture_error = error

        self._fixture_thread = threading.Thread(target=receive, daemon=True)
        self._fixture_thread.start()
        return super().launch(config, prepared)

    def _completion_is_failure(self, completion: bytes, failure_markers: tuple[bytes, ...]) -> bool:
        return completion in failure_markers

    def close(self) -> None:
        thread = self._fixture_thread
        if thread is not None:
            self._fixture.cancel()
            thread.join(2.0)
        self._fixture_thread = None
        self._fixture.__exit__(None, None, None)
        super().close()

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        if self._fixture_thread is not None:
            self._fixture_thread.join(1.0)
        summary = dict(self._fixture_summary or {})
        result["single_packet_fixture"] = summary
        result["single_packet_fixture_error"] = str(self._fixture_error) if self._fixture_error else None
        if result.get("passed") is True:
            try:
                result.update(
                    validate_packet_exchange(
                        transcript.decode("ascii", errors="replace"),
                        summary,
                        self.nonce,
                    )
                )
            except (TypeError, ValueError) as error:
                result["passed"] = False
                result["reason"] = str(error)
        self._require_output().atomic_write(
            "single-packet-fixture.json",
            (json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        super().publish(config, prepared, transcript, result)


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), SinglePacketQemuOperations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config,
                operations,
                classifier=lambda transcript, expected_debian_release: classify_single_packet_qemu(
                    transcript, nonce=operations.nonce
                ),
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(f"single-packet-qemu-gate: terminated by signal {error.signum}")
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"single-packet-qemu-gate: {reason}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
