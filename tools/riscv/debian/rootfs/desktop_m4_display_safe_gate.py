#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the M4 display-safe boot contract without a host display backend.

The guest framebuffer evidence and ordered startup phases are still mandatory.
QEMU in the development container exposes only ``-display none``; its HMP
``screendump`` therefore cannot provide an independent host pixel surface.
Keep that limitation explicit instead of treating a synthetic single-color
PPM as a successful scanout result.
"""

from __future__ import annotations

import sys

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DesktopM4Operations,
    classify_desktop_m4,
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


class DesktopM4DisplaySafeOperations(DesktopM4Operations):
    """Use the M4 protocol while relying on guest fb0 evidence in headless QEMU."""

    ARTIFACT_PREFIX = "desktop-m4-display-safe"
    CAPTURE_SCREENSHOT = False


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM4DisplaySafeOperations(config) as operations:
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
            f"debian-desktop-m4-display-safe-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-display-safe-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
