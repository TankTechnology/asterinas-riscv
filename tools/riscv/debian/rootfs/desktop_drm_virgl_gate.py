#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Gate the Debian desktop profile on the virgl (3D) virtio-gpu device.

This reuses the desktop-drm rootfs and boot protocol but attaches
`virtio-gpu-gl-device` behind a host GL context, so Mesa loads its virgl
Gallium driver and Xorg glamor runs on real GPU commands instead of
llvmpipe.  The extra `DEBIAN_DESKTOP_DRM_GL renderer=virgl` milestone comes
from the in-guest evidence script's `glxinfo` probe.
"""

from __future__ import annotations

import sys
from typing import Any

from tools.riscv.debian.rootfs.desktop_drm_gate import (
    DESKTOP_DRM_VIRGL_MILESTONES,
    DesktopDRMOperations,
    desktop_drm_virgl_qemu_argv,
    orchestrate_desktop_drm_gate,
)
from tools.riscv.debian.rootfs.desktop_m3_gate import classify_desktop
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output


def classify_desktop_drm_virgl(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_DRM_VIRGL_MILESTONES,
        failure_marker=b"DEBIAN_DESKTOP_DRM_FAIL reason=",
    )


class DesktopDrmVirglOperations(DesktopDRMOperations):
    """Same signed-root lifecycle, but with the 3D device and GL evidence."""

    ARTIFACT_PREFIX = "desktop-drm-virgl"
    MILESTONES = DESKTOP_DRM_VIRGL_MILESTONES
    # QEMU cannot screendump an egl-headless GL console ("Error: no surface"),
    # and VNC readback does not deliver frames for an active virgl scanout
    # either.  Like the Xfce virgl gate, the proof is guest-reported: the
    # glxinfo renderer marker and the Xorg glamor lines in the transcript.
    CAPTURE_SCREENSHOT = False

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_drm_virgl_qemu_argv(**arguments)


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopDrmVirglOperations(config) as operations:
            result = orchestrate_desktop_drm_gate(
                config, operations, classifier=classify_desktop_drm_virgl
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-drm-virgl-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = (
            error.reason
            if isinstance(error, GateFailure)
            else f"{type(error).__name__}: {error}"
        )
        print(f"debian-desktop-drm-virgl-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
