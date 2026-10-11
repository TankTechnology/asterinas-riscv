#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Boot the desktop once, arm the guest reboot timer, and verify firmware recovery."""

from __future__ import annotations

import sys
import time
from typing import Any

from tools.riscv.debian.rootfs.desktop_m3_gate import DESKTOP_M3_BOOTARGS
from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_MILESTONES,
    DesktopM4Operations,
    classify_desktop,
    desktop_m4_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
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


# The desktop probe may spend up to its bounded five-second settle interval
# plus the first Xorg/client start.  Keep the reboot marker comfortably after
# the readiness milestone so recovery tests exercise a real desktop epoch
# rather than racing the probe itself.
RECOVERY_SECONDS = 300
RECOVERY_BOOTARGS = DESKTOP_M3_BOOTARGS.replace(
    " -- --root-init=systemd",
    " asterinas.reboot_after={} "
    "systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered "
    "systemd.setenv=ASTERINAS_DESKTOP_M4_FRAMEBUFFER_PROBE=/bin/true"
    " -- --root-init=systemd".format(RECOVERY_SECONDS),
)


def classify_desktop_m4_recovery(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require desktop evidence followed by a fresh firmware/U-Boot epoch."""

    # Pixel sampling has its own gate.  This recovery gate deliberately keeps
    # the desktop process set and firmware epoch independent of that probe so
    # a transient guest file-read failure cannot hide reboot recoverability.
    result = classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_M4_MILESTONES,
        failure_marker=b"DEBIAN_DESKTOP_M4_FAIL reason=",
    )
    if not result.passed:
        return result
    ready = transcript.find(b"DEBIAN_DESKTOP_M4_READY")
    if ready < 0:
        return GateResult(False, "missing desktop readiness marker", None)
    fresh = transcript.find(b"OpenSBI v", ready + 1)
    if fresh < 0:
        return GateResult(False, "missing post-desktop OpenSBI epoch", None)
    if transcript.find(b"U-Boot", fresh) < 0 or transcript.find(b"=> ", fresh) < 0:
        return GateResult(False, "missing post-desktop U-Boot prompt", None)
    return GateResult(True, "pass", None)


class DesktopM4RecoveryOperations(DesktopM4Operations):
    """Reuse the desktop boot protocol with one bounded software reboot."""

    SCHEMA_VERSION = 4
    ARTIFACT_PREFIX = "desktop-m4-recovery"
    CAPTURE_SCREENSHOT = False
    BOOTARGS = RECOVERY_BOOTARGS

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        arguments["allow_reboot"] = True
        return desktop_m4_qemu_argv(**arguments)

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        super().run_protocol(session, config)
        session["recovery_start"] = session["serial"].checkpoint()

    def request_quit(self, session: dict[str, Any], config: GateConfig) -> None:
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
        with TerminationSignalState(), DesktopM4RecoveryOperations(config) as operations:
            result = orchestrate_systemd_m2_gate(
                config,
                operations,
                classifier=classify_desktop_m4_recovery,
            )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m4-recovery-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m4-recovery-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
