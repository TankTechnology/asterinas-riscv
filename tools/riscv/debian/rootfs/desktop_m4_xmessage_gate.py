#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the desktop gate with a temporary large X client for scanout diagnosis."""

from __future__ import annotations

import sys

from tools.riscv.debian.rootfs.desktop_m3_gate import DESKTOP_M3_BOOTARGS
from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DesktopM4Operations,
    orchestrate_desktop_m4_gate,
)
from tools.riscv.debian.rootfs.gate_runtime import (
    GateTermination,
    TerminationSignalState,
)
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output


DIAGNOSTIC_BOOTARGS = DESKTOP_M3_BOOTARGS.replace(
    " -- --root-init=systemd",
    " systemd.setenv=ASTERINAS_DESKTOP_M4_SCANOUT_DIAGNOSTIC=1"
    " systemd.setenv=ASTERINAS_DESKTOP_M4_FRAMEBUFFER_TRACE=1"
    " systemd.setenv=ASTERINAS_DESKTOP_M4_FRAMEBUFFER_FLUSH=1"
    " systemd.setenv=ASTERINAS_DESKTOP_M4_FRAMEBUFFER_FLUSH_BYTES=4096"
    " -- --root-init=systemd",
)


class DesktopM4XmessageOperations(DesktopM4Operations):
    """Keep the normal M4 gate contract and enable one diagnostic X client."""

    ARTIFACT_PREFIX = "desktop-m4-xmessage"
    BOOTARGS = DIAGNOSTIC_BOOTARGS


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM4XmessageOperations(config) as ops:
            result = orchestrate_desktop_m4_gate(config, ops)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m4-xmessage-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-xmessage-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
