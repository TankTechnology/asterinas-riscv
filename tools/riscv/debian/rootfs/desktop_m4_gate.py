#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Cold-boot the signed Debian application desktop and capture visual evidence."""

from __future__ import annotations

import sys
from typing import Any

from tools.riscv.debian.rootfs.desktop_m3_gate import (
    DESKTOP_M3_BOOTARGS,
    DesktopM3Operations,
    classify_desktop,
    desktop_m3_qemu_argv,
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


DESKTOP_M4_MILESTONES = (
    "DEBIAN_DESKTOP_M4_UDEV state=active",
    "DEBIAN_DESKTOP_M4_LOGIND state=active",
    "DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0",
    "DEBIAN_DESKTOP_M4_SHELL wallpaper=asterinas desktop=pcmanfm panel=lxpanel launchers=3",
    "DEBIAN_DESKTOP_M4_CLIENTS window-manager=openbox file-manager=pcmanfm browser=netsurf terminal=xterm",
    "DEBIAN_DESKTOP_M4_READY user=asterinas display=:0",
)


def desktop_m4_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Return the same frozen graphical QEMU device contract used by M3."""

    return desktop_m3_qemu_argv(**arguments)


def classify_desktop_m4(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require ordered application evidence across the fully drained transcript."""

    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_M4_MILESTONES,
        failure_marker=b"DEBIAN_DESKTOP_M4_FAIL reason=",
    )


class DesktopM4Operations(DesktopM3Operations):
    """Desktop M3 mechanics bound to the immutable M4 application profile."""

    SCHEMA_VERSION = 4
    PROFILE_NAME = "desktop-m4"
    ARTIFACT_PREFIX = "desktop-m4"
    MILESTONES = DESKTOP_M4_MILESTONES
    FAILURE_MARKER = b"DEBIAN_DESKTOP_M4_FAIL reason="
    BOOTARGS = DESKTOP_M3_BOOTARGS

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_m4_qemu_argv(**arguments)


def orchestrate_desktop_m4_gate(
    config: GateConfig, operations: DesktopM4Operations
) -> dict[str, object]:
    """Reuse the bounded graphical lifecycle with the M4 classifier."""

    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m4,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM4Operations(config) as operations:
            result = orchestrate_desktop_m4_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m4-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
