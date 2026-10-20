#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the DRM desktop and require an authenticated Firefox WebRender run."""

from __future__ import annotations

import re
import sys

from tools.riscv.debian.rootfs.desktop_m4_drm_recovery_gate import (
    DesktopM4DrmRecoveryOperations,
    RECOVERY_BOOTARGS,
    classify_desktop_m4_drm_recovery,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


_FIREFOX_SUPPORT = re.compile(rb"FIREFOX_DRM_SUPPORT [^\r\n]+")
_FIREFOX_GL = re.compile(rb"FIREFOX_DRM_GL renderer=([^\r\n]+)")
_FIREFOX_GLXTEST_RENDER_NODE = re.compile(
    rb"FIREFOX_DRM_GLXTEST [^\r\n]*DRM_RENDERDEVICE /dev/dri/renderD[0-9]+"
)
_FIREFOX_EGL_DEVICE = re.compile(
    rb"FIREFOX_DRM_EGL_PROBE [^\r\n]*"
    rb"(?:display=(?:gbm|device)|EGL_DEVICE /dev/dri/renderD[0-9]+)"
)
_FIREFOX_SCREENSHOT = re.compile(
    rb"FIREFOX_DRM_SCREENSHOT width=[1-9][0-9]* height=[1-9][0-9]* "
    rb"sha256=[0-9a-f]{64}"
)
_FIREFOX_FRAMES = re.compile(
    rb"FIREFOX_DRM_FRAMES samples=[1-9][0-9]* "
    rb"min_ms=[0-9]+(?:\.[0-9]+)? p50_ms=[0-9]+(?:\.[0-9]+)? "
    rb"p95_ms=[0-9]+(?:\.[0-9]+)? max_ms=[0-9]+(?:\.[0-9]+)?"
)
_FIREFOX_RECOVERY = re.compile(rb"FIREFOX_DRM_RECOVERY restart=pass")
_SOFTWARE_RENDERERS = (b"llvmpipe", b"softpipe", b"swrast", b"swgl")


def _is_non_software_renderer(value: bytes) -> bool:
    """Accept truthful renderer names other than known software renderers."""

    lowered = value.strip().lower()
    return bool(lowered) and lowered not in (b"none", b"unreported") and not any(
        marker in lowered for marker in _SOFTWARE_RENDERERS
    )


def _firefox_acceleration_evidence(epoch: bytes) -> tuple[str, ...]:
    """Collect the independent acceleration proofs from one Firefox epoch.

    Scheme A intentionally does not require Firefox's sanitizer to spell the
    renderer ``virgl``.  It still rejects explicit software renderers and
    requires at least two independent proofs from the four evidence classes.
    """

    evidence: list[str] = []
    support = _FIREFOX_SUPPORT.search(epoch)
    if support:
        support_line = support.group(0).lower()
        renderer_match = re.search(rb"\brenderer=([^ \r\n]+)", support_line)
        if renderer_match and _is_non_software_renderer(renderer_match.group(1)):
            evidence.append("renderer")
        if b"webrender=available" in support_line or b"compositing=webrender" in support_line:
            evidence.append("webrender")

    gl = _FIREFOX_GL.search(epoch)
    if gl and _is_non_software_renderer(gl.group(1)):
        evidence.append("renderer")

    if _FIREFOX_GLXTEST_RENDER_NODE.search(epoch) or _FIREFOX_EGL_DEVICE.search(epoch):
        evidence.append("drm-render-node")

    if _FIREFOX_FRAMES.search(epoch) or re.search(
        rb"FIREFOX_DRM_MOZLOG [^\r\n]*(?:RenderCompositorOGL|WebRender)", epoch
    ):
        evidence.append("webrender")

    if _FIREFOX_SCREENSHOT.search(epoch):
        evidence.append("screenshot")

    return tuple(dict.fromkeys(evidence))


def classify_firefox_drm_recovery(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require the desktop/recovery contract plus ordered Firefox evidence."""

    desktop = classify_desktop_m4_drm_recovery(
        transcript,
        expected_debian_release=expected_debian_release,
        require_x11_screenshot=False,
    )
    if not desktop.passed:
        return desktop
    ready = transcript.find(b"DEBIAN_DESKTOP_M4_READY")
    fresh = transcript.find(b"OpenSBI v", ready + 1)
    if ready < 0 or fresh < 0:
        return GateResult(False, "missing Firefox desktop epoch boundaries", None)
    epoch = transcript[ready:fresh]
    strong_software = re.search(
        rb"FIREFOX_DRM_(?:SUPPORT|GL|GLXTEST|EGL_PROBE|SCENE_STATE) [^\r\n]*"
        rb"(?:llvmpipe|softpipe|swrast|swgl)",
        epoch,
        re.IGNORECASE,
    )
    if strong_software:
        return GateResult(False, "Firefox epoch contains a software renderer", None)
    evidence = _firefox_acceleration_evidence(epoch)
    if len(evidence) < 2:
        return GateResult(
            False,
            "insufficient Firefox acceleration evidence "
            f"(need=2 have={','.join(evidence) or 'none'})",
            None,
        )
    if not _FIREFOX_RECOVERY.search(epoch):
        return GateResult(False, "missing Firefox recovery marker", None)
    return GateResult(True, "pass evidence=" + ",".join(evidence), None)


class FirefoxDrmRecoveryOperations(DesktopM4DrmRecoveryOperations):
    """Reuse the bounded DRM lifecycle with the Firefox-specific classifier."""

    ARTIFACT_PREFIX = "firefox-drm-recovery"

    @staticmethod
    def _qemu_argv(**arguments: object) -> tuple[str, ...]:
        """Use MTTCG so Firefox's cold RISC-V startup can make progress."""

        argv = list(DesktopM4DrmRecoveryOperations._qemu_argv(**arguments))
        machine = argv.index("-machine")
        argv[machine:machine] = ["-accel", "tcg,thread=multi"]
        return tuple(argv)

    # Firefox's cold RISC-V TCG start is independently bounded at 600 s.
    # Leave one software-reboot deadline for that run plus recovery, rather
    # than letting the ordinary 180 s desktop deadline cut the browser off.
    BOOTARGS = RECOVERY_BOOTARGS.replace(
        "asterinas.reboot_after=180", "asterinas.reboot_after=900"
    ).replace(
        "asterinas.reboot_after=600", "asterinas.reboot_after=1200"
    ).replace(
        "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered",
        "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "
        "systemd.setenv=ASTERINAS_DESKTOP_BROWSER_ENABLED=0 "
        "systemd.setenv=ASTERINAS_DESKTOP_M4_SKIP_DRM_X11_SCREENSHOT=1",
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), FirefoxDrmRecoveryOperations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config,
                operations,
                classifier=classify_firefox_drm_recovery,
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"firefox-drm-recovery-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"firefox-drm-recovery-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
