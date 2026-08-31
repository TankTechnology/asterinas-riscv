#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded QEMU startup probe for the Debian Firefox M5 profile."""

from __future__ import annotations

import sys
from typing import Any

from tools.riscv.debian.rootfs.desktop_m3_gate import DesktopM3Operations
from tools.riscv.debian.rootfs.browser_m5_qemu_gate import BrowserM5QemuOperations
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


STARTUP_READY_MARKER = (
    "DEBIAN_BROWSER_M5_STARTUP_READY firefox=esr xorg=fbdev "
    "marionette=loopback sandbox=normal"
)
BROWSER_M5_STARTUP_MILESTONES = (STARTUP_READY_MARKER,)
_STARTUP_FAILURE = b"DEBIAN_BROWSER_M5_STARTUP_FAIL reason="
_BROWSER_FAILURE = b"DEBIAN_BROWSER_M5_FAIL reason="
_NETWORK_FAILURE = b"DEBIAN_NETWORK_M5_FAIL reason="
_NETNS_FAILURE = b"DEBIAN_BROWSER_M5_NETNS_FAIL reason="


def validate_checkpoint_timeout(value: object) -> int:
    """Validate the short startup budget independently of full gate budgets."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("startup checkpoint budget must be an integer")
    if value != int(value) or not 0 < int(value) <= 600:
        raise ValueError("startup checkpoint budget must be in the range 1..600")
    return int(value)


def classify_startup(transcript: bytes, *, expected_debian_release: str) -> GateResult:
    """Classify only Firefox/Xorg readiness, never the content gate."""

    if not expected_debian_release:
        return GateResult(False, "missing expected Debian release", None)
    clean = transcript.lower()
    if b"firefox-process-exit" in clean:
        return GateResult(False, "firefox process exited", None)
    if b"xorg-or-input" in clean:
        return GateResult(False, "Xorg or input failed", None)
    if _NETWORK_FAILURE.lower() in clean or _NETNS_FAILURE.lower() in clean:
        return GateResult(False, "network or namespace failure", None)
    if _BROWSER_FAILURE.lower() in clean:
        return GateResult(False, "browser guest failure", None)

    marker = STARTUP_READY_MARKER.encode()
    count = transcript.count(marker)
    if count > 1:
        return GateResult(False, "duplicate startup milestone", None)
    if count == 0:
        return GateResult(False, "timeout", None)
    return GateResult(True, "ready", None)


class BrowserM5StartupOperations(BrowserM5QemuOperations):
    """Reuse the graphical QEMU lifecycle but stop at startup readiness."""

    ARTIFACT_PREFIX = "browser-m5-startup"
    MILESTONES = BROWSER_M5_STARTUP_MILESTONES
    CAPTURE_SCREENSHOT = False

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        reason = str(result.get("reason", ""))
        if reason in {"protocol", "guest reported desktop failure"}:
            classified = classify_startup(
                transcript,
                expected_debian_release=str(result.get("debian_release", "")),
            )
            result["reason"] = classified.reason
        result["checkpoint_seconds"] = validate_checkpoint_timeout(config.boot_timeout)
        result["milestones"] = list(BROWSER_M5_STARTUP_MILESTONES)
        # Startup does not require the network fixture to finish. Keep the
        # fixture server lifecycle inherited from BrowserM5QemuOperations, but
        # publish only the startup transcript/result.
        DesktopM3Operations.publish(self, config, prepared, transcript, result)


def orchestrate_browser_m5_startup_probe(
    config: GateConfig, operations: BrowserM5StartupOperations
) -> dict[str, object]:
    """Run one bounded graphical boot and quit as soon as startup is ready."""

    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_startup,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        validate_checkpoint_timeout(config.boot_timeout)
        _safe_output(config.output_directory)
        with TerminationSignalState(), BrowserM5StartupOperations(config) as operations:
            result = orchestrate_browser_m5_startup_probe(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-browser-m5-startup-probe: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-browser-m5-startup-probe: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
