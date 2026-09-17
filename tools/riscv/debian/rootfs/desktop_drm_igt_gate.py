#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Gate the Debian DRM driver on a curated IGT GPU Tools subset.

The guest boots the desktop-drm rootfs with `asterinas.igt=1`, which keeps
the desktop session off (KMS tests need the DRM master) and starts the
`asterinas-igt.service` runner instead.  The runner executes the curated
IGT subset from /opt/igt and reports one marker per test plus a final
summary; this gate classifies the transcript.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from tools.riscv.debian.rootfs.desktop_drm_gate import (
    DESKTOP_DRM_BOOTARGS,
    DesktopDRMOperations,
    desktop_drm_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate

# Boots the desktop-drm rootfs into a dedicated systemd target that runs the
# IGT subset instead of the desktop session.  `systemd.unit=` is read from
# /proc/cmdline by systemd itself, so the stage-1 init's strict argv contract
# (only --root-init= after `--`) is untouched.
IGT_BOOTARGS = DESKTOP_DRM_BOOTARGS.replace(
    " -- ", " systemd.unit=asterinas-igt.target -- "
)
IGT_BEGIN_MARKER = "ASTERINAS_IGT_BEGIN"
IGT_DONE_RE = re.compile(r"ASTERINAS_IGT_DONE pass=(\d+) skip=(\d+) fail=(\d+)")
IGT_RESULT_RE = re.compile(
    r"ASTERINAS_IGT_RESULT test=(\S+) rc=(\d+) status=(PASS|SKIP|FAIL)"
)

# These tests exercise the feature set the driver claims (see
# tools/riscv/drm/VALIDATION.md), so they must pass, not skip.
IGT_REQUIRED_PASS = (
    "drm_virtgpu",
    "dumb_buffer",
    "syncobj_basic",
    "kms_addfb_basic",
)


def classify_drm_igt(transcript: bytes, *, expected_debian_release: str) -> GateResult:
    """Classify the IGT transcript: no FAILs and every required test passed."""

    text = transcript.decode("utf-8", errors="replace")
    lowered = text.lower()
    for marker, reason in (
        ("kernel panic", "kernel panic"),
        ("asterinas_igt_fail reason=", "IGT runner failure"),
    ):
        if marker in lowered:
            return GateResult(False, reason, None)

    results = dict()
    for match in IGT_RESULT_RE.finditer(text):
        results[match.group(1)] = match.group(3)

    done = IGT_DONE_RE.search(text)
    if done is None:
        return GateResult(False, "missing IGT summary marker", None)
    fail_count = int(done.group(3))
    if fail_count != 0:
        return GateResult(False, f"IGT failures: {fail_count}", None)

    missing = [name for name in IGT_REQUIRED_PASS if results.get(name) != "PASS"]
    if missing:
        return GateResult(False, f"required IGT tests did not pass: {missing}", None)
    return GateResult(True, "pass", None)


class DesktopDrmIgtOperations(DesktopDRMOperations):
    """Boot the desktop-drm rootfs into the IGT runner instead of Xorg."""

    ARTIFACT_PREFIX = "desktop-drm-igt"
    BOOTARGS = IGT_BOOTARGS
    MILESTONES = (IGT_BEGIN_MARKER, "ASTERINAS_IGT_DONE")
    FAILURE_MARKER = b"ASTERINAS_IGT_FAIL reason="
    # The IGT gate has no display session; the evidence is the transcript.
    CAPTURE_SCREENSHOT = False


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopDrmIgtOperations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config, operations, classifier=classify_drm_igt
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-drm-igt-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-drm-igt-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
