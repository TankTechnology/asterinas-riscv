#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the DRM desktop once and require its bounded firmware recovery epoch."""

from __future__ import annotations

import sys
import time
from typing import Any

from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_DRM_BOOTARGS,
    DesktopM4DrmOperations,
    classify_desktop_m4_drm,
    desktop_m4_drm_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


# Keep one bounded recovery timer. Under RISC-V TCG, the first systemd boot
# can spend several minutes in sysctl/udev/tmphile jobs before Xorg/glamor and
# the virgl pixel probe are scheduled. 180 seconds cuts that valid startup off
# and misclassifies it as a display failure. Replace the inherited timer
# instead of appending a second one: the kernel parser must see one deadline.
RECOVERY_SECONDS = 600
RECOVERY_BOOTARGS = DESKTOP_M4_DRM_BOOTARGS.replace(
    "asterinas.reboot_after=300", f"asterinas.reboot_after={RECOVERY_SECONDS}"
)


def classify_desktop_m4_drm_recovery(
    transcript: bytes,
    *,
    expected_debian_release: str,
    require_x11_screenshot: bool = True,
) -> GateResult:
    """Require one complete DRM desktop epoch followed by firmware recovery."""

    ready = transcript.find(b"DEBIAN_DESKTOP_M4_READY")
    if ready < 0:
        return GateResult(False, "missing DRM desktop readiness marker", None)
    fresh = transcript.find(b"OpenSBI v", ready + 1)
    if fresh < 0:
        return GateResult(False, "missing post-desktop OpenSBI epoch", None)

    first_epoch = transcript[:fresh]
    result = classify_desktop_m4_drm(
        first_epoch,
        expected_debian_release=expected_debian_release,
        require_x11_screenshot=require_x11_screenshot,
    )
    if not result.passed:
        return result
    recovery = transcript[fresh:]
    if b"U-Boot" not in recovery or b"=> " not in recovery:
        return GateResult(False, "missing post-desktop U-Boot prompt", None)
    return GateResult(True, "pass", None)


class DesktopM4DrmRecoveryOperations(DesktopM4DrmOperations):
    """Reuse the DRM one-boot protocol and wait for one automatic reboot."""

    ARTIFACT_PREFIX = "desktop-m4-drm-recovery"
    BOOTARGS = RECOVERY_BOOTARGS

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        arguments["allow_reboot"] = True
        return desktop_m4_drm_qemu_argv(**arguments)

    def run_protocol(self, session: dict[str, Any], config: Any) -> None:
        super().run_protocol(session, config)
        session["recovery_start"] = session["serial"].checkpoint()

    def request_quit(self, session: dict[str, Any], config: Any) -> None:
        serial = session["serial"]
        deadline = time.monotonic() + config.boot_timeout
        start = int(session["recovery_start"])
        serial.wait_for(b"OpenSBI v", deadline, start=start)
        serial.wait_for(b"U-Boot", deadline, start=start)
        serial.wait_for(b"=> ", deadline, start=start)
        super().request_quit(session, config)


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with (
            TerminationSignalState(),
            DesktopM4DrmRecoveryOperations(config) as operations,
        ):
            result = orchestrate_systemd_m2_gate(
                config,
                operations,
                classifier=classify_desktop_m4_drm_recovery,
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m4-drm-recovery-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-drm-recovery-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
