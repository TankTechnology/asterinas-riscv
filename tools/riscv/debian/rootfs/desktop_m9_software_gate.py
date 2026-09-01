#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the Debian desktop software smoke test after the desktop/network gates."""

from __future__ import annotations

import sys

from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
    classify_desktop_m5_qemu,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
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


DESKTOP_M8_READY_MARKER = "DEBIAN_BROWSER_M8_READY quality=lightweight"
DESKTOP_M9_SOFTWARE_READY_MARKER = (
    "DEBIAN_DESKTOP_M9_SOFTWARE_READY "
    "vim=pass ffmpeg=pass ffprobe=pass media=pass"
)
DESKTOP_M9_FAILURE_MARKER = b"DEBIAN_DESKTOP_M9_FAIL reason="


def classify_desktop_m9_software(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require complete desktop/network and software evidence."""

    base = classify_desktop_m5_qemu(
        transcript,
        expected_debian_release=expected_debian_release,
    )
    if not base.passed:
        return base
    if DESKTOP_M9_FAILURE_MARKER.lower() in transcript.lower():
        return GateResult(False, "software guest failure", None)

    marker = DESKTOP_M9_SOFTWARE_READY_MARKER.encode()
    if transcript.count(marker) != 1:
        return GateResult(False, "missing or duplicate software evidence", None)
    if transcript.find(marker) < transcript.find(
        DESKTOP_M5_QEMU_MILESTONES[-1].encode()
    ):
        return GateResult(False, "software milestones out of order", None)
    return GateResult(True, "pass", None)


class DesktopM9SoftwareOperations(DesktopM5QemuOperations):
    """Reuse the bounded M5 QEMU lifecycle with a software-specific identity."""

    SCHEMA_VERSION = 5
    PROFILE_NAME = "desktop-m9-software"
    ARTIFACT_PREFIX = "desktop-m9-software-qemu"
    MILESTONES = (
        *DESKTOP_M5_QEMU_MILESTONES,
        *DESKTOP_M4_MILESTONES,
        DESKTOP_M9_SOFTWARE_READY_MARKER,
    )
    FAILURE_MARKER = DESKTOP_M9_FAILURE_MARKER
    ADDITIONAL_FAILURE_MARKERS = (
        b"DEBIAN_ROOTFS_FAIL reason=",
        b"DEBIAN_DESKTOP_M4_FAIL reason=",
        b"DEBIAN_NETWORK_M5_FAIL reason=",
    )
    _qemu_argv = staticmethod(desktop_m5_qemu_argv)


def orchestrate_desktop_m9_software_gate(
    config: GateConfig, operations: DesktopM9SoftwareOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m9_software,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM9SoftwareOperations(config) as operations:
            result = orchestrate_desktop_m9_software_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m9-software-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m9-software-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
