#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Qualify a deterministic fbdev scanout separately from application painting."""

import re

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DesktopM4Operations,
    classify_desktop_m4,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


class FbdevCanaryOperations(DesktopM4Operations):
    ARTIFACT_PREFIX = "desktop-m4-fbdev-canary"
    EARLY_CAPTURE_MARKER = b"DEBIAN_DESKTOP_M4_FB_SCANOUT_READY"
    # Root-init arguments follow the ``--`` delimiter.  Put the environment
    # assignment before it; otherwise the Asterinas root-init parser treats
    # the assignment as an unknown init argument and aborts before systemd.
    BOOTARGS = DesktopM4Operations.BOOTARGS.replace(
        " -- --root-init=systemd",
        " systemd.setenv=ASTERINAS_DESKTOP_M4_SCANOUT_PATTERN=1 -- --root-init=systemd",
    )

    def publish(self, config, prepared, transcript, result):
        result["qualification"] = "fbdev-pattern-only"
        result["application_paint_verified"] = False
        super().publish(config, prepared, transcript, result)


def classify_canary(transcript, *, expected_debian_release):
    result = classify_desktop_m4(
        transcript, expected_debian_release=expected_debian_release
    )
    if not result.passed:
        return result
    records = re.findall(
        rb"DEBIAN_DESKTOP_M4_FB_SCANOUT_RESTORED width=1280 height=1024 "
        rb"saved_sha256=([0-9a-f]{64}) restored_sha256=([0-9a-f]{64}) status=pass",
        transcript,
    )
    if len(records) != 1 or records[0][0] != records[0][1]:
        return GateResult(False, "missing exact framebuffer restore", None)
    return result


def main(arguments=None):
    config = parse_gate_args(arguments)
    _safe_output(config.output_directory)
    with TerminationSignalState(), FbdevCanaryOperations(config) as operations:
        result = orchestrate_systemd_m2_gate(config, operations, classifier=classify_canary)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
