#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Prove Debian M5 HTTPS and application desktop behavior through QEMU slirp."""

from __future__ import annotations

import json
import sys
from typing import Any

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_MILESTONES,
    DesktopM4Operations,
    desktop_m4_qemu_argv,
)
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
    classify_desktop_m5_qemu,
)
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


QEMU_FIXTURE_PORT = 17894
QEMU_FIXTURE_REQUESTS = 20
QEMU_NETWORK_TIMEOUT_SECONDS = 120
QEMU_FIXTURE_URL = f"http://10.0.2.2:{QEMU_FIXTURE_PORT}{FIXTURE_PATH}"
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

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        summary = self.fixture.summary()
        result["network_fixture"] = summary
        if result.get("passed") is True and not is_successful_summary(
            summary, expected_requests=QEMU_FIXTURE_REQUESTS
        ):
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


def orchestrate_desktop_m5_qemu_gate(
    config: GateConfig, operations: DesktopM5QemuOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m5_qemu,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM5QemuOperations(config) as operations:
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
