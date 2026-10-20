#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Verify the fbdev guest framebuffer through QEMU's VNC scanout surface."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DesktopM4Operations,
    classify_desktop_m4,
    desktop_m4_qemu_argv,
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


class DesktopM4ScanoutOperations(DesktopM4Operations):
    """Run the M4 guest against a private, host-readable VNC surface."""

    ARTIFACT_PREFIX = "desktop-m4-scanout"
    # Make the host capture deterministic.  Normal Xorg/client startup is
    # intentionally kept in the separate desktop gate: its first paint can
    # remain black while clients are still mapping windows.  The bounded
    # framebuffer pattern is written by the guest through /dev/fb0 and held
    # until the host captures it, so a non-blank PPM proves the complete
    # guest-fbdev -> Bochs BAR -> QEMU display backend path.
    EARLY_CAPTURE_MARKER = b"DEBIAN_DESKTOP_M4_FB_SCANOUT_READY"
    BOOTARGS = DesktopM4Operations.BOOTARGS.replace(
        " -- --root-init=systemd",
        " systemd.setenv=ASTERINAS_DESKTOP_M4_SCANOUT_PATTERN=1 -- --root-init=systemd",
    )

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        monitor_socket = Path(arguments["monitor_socket"])
        arguments["vnc_socket"] = monitor_socket.with_name("display.vnc")
        return desktop_m4_qemu_argv(**arguments)


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM4ScanoutOperations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config,
                operations,
                classifier=classify_desktop_m4,
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m4-scanout-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-scanout-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
