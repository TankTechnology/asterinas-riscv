#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Prove Debian M5 HTTPS and application desktop behavior through QEMU slirp."""

from __future__ import annotations

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


DESKTOP_M5_QEMU_BOOTARGS = (
    "console=ttyS0 loglevel=4 init=/init "
    "asterinas.debian_network=qemu-slirp -- --root-init=systemd"
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
