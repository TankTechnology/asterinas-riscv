#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the Debian M4 desktop lifecycle through the virtio-gpu GL provider."""

from __future__ import annotations

import sys
from pathlib import Path

# Keep direct host invocation usable without requiring callers to manufacture
# PYTHONPATH. Guest-installed copies never execute this branch.
if __package__ in {None, ""}:
    repository_root = Path(__file__).resolve().parents[4]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DesktopM4DrmOperations,
    classify_desktop_m4_drm,
)
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM4DrmOperations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config,
                operations,
                classifier=lambda transcript, expected_debian_release: classify_desktop_m4_drm(
                    transcript,
                    expected_debian_release=expected_debian_release,
                    require_x11_screenshot=not operations._firefox_profile,
                ),
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m4-drm-gate: terminated by signal {error.signum}",
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-drm-gate: {reason}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

