#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""QEMU desktop gate with a deterministic X11-to-fb0 paint assertion."""

from __future__ import annotations

import re
import sys

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DesktopM4Operations,
    classify_desktop_m4,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


X11_BOOTARGS = DesktopM4Operations.BOOTARGS.replace(
    " -- --root-init=systemd",
    " systemd.setenv=ASTERINAS_DESKTOP_M4_X11_PAINT_TEST=1 -- --root-init=systemd",
)
X11_SAMPLE = re.compile(
    rb"DEBIAN_DESKTOP_M4_X11_SAMPLE band=([0-2]) "
    rb"expected=([0-9a-f]{6}) ximage=([0-9a-f]{6}) fb0=([0-9a-f]{6})"
)
EXPECTED = (b"cc6622", b"22cc66", b"6622cc")


def classify_x11_desktop(transcript: bytes, *, expected_debian_release: str) -> GateResult:
    result = classify_desktop_m4(
        transcript, expected_debian_release=expected_debian_release
    )
    if not result.passed:
        return result
    records = X11_SAMPLE.findall(transcript)
    if len(records) != 3:
        return GateResult(False, "missing X11 paint samples", None)
    for index, (band, expected, ximage, fb0) in enumerate(records):
        if (
            int(band) != index
            or expected != EXPECTED[index]
            or ximage != expected
            or fb0 != expected
        ):
            return GateResult(False, "X11 paint sample mismatch", None)
    if b"DEBIAN_DESKTOP_M4_X11_READY bands=3" not in transcript:
        return GateResult(False, "missing X11 paint ready marker", None)
    return result


class X11DesktopOperations(DesktopM4Operations):
    ARTIFACT_PREFIX = "desktop-m4-x11"
    BOOTARGS = X11_BOOTARGS
    EARLY_CAPTURE_MARKER = b"DEBIAN_DESKTOP_M4_X11_READY"


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), X11DesktopOperations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config, operations, classifier=classify_x11_desktop
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m4-x11-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-x11-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
