#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Gate the opt-in root serial console through the graphical QEMU lifecycle."""

from __future__ import annotations

import secrets
import sys
import time
from dataclasses import asdict
from typing import Any, Mapping

from tools.riscv.debian.rootfs.debug_console_protocol import (
    DEBUG_CONSOLE_READY,
    DebugConsoleEvidence,
    DebugConsoleProtocolError,
    run_debug_console_phase,
)
from tools.riscv.debian.rootfs.desktop_m3_gate import capture_rendered_ppm
from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    NetworkMode,
    classify_desktop_m5_qemu,
    classify_web_network,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DesktopM5QemuOperations,
    qemu_web_network_bootargs,
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


DEBUG_CONSOLE_QEMU_MILESTONES = (
    "DEBIAN_STAGE1_PROGRESS step=probe-complete result=match",
    "DEBIAN_STAGE1_PROGRESS step=handoff-done action=debug-console",
    DEBUG_CONSOLE_READY,
)
_DEBUG_CONSOLE_QEMU_FATAL_MARKERS = (
    (b"debian_rootfs_fail reason=", "stage1 failure"),
    (b"debian_network_m5_fail reason=", "network guest failure"),
    (b"debian_browser_web_fail reason=", "browser guest failure"),
    (b"kernel panic", "kernel panic"),
    (b"uncaught panic:", "kernel panic"),
    (b"ext2-fs error", "ext2 error"),
    (b"buffer i/o error", "block I/O error"),
)
_WEB_NETWORK_READY = b"DEBIAN_WEB_NETWORK_READY mode=direct layers=10"
_WEB_DESKTOP_READY = b"BROWSER_WEB_DESKTOP_STAGE=x-socket-ready"
_M5_DESKTOP_READY = DESKTOP_M4_MILESTONES[-1].encode()
_POST_CONSOLE_FAILURE_MARKERS = (
    b"DEBIAN_ROOTFS_FAIL reason=",
    b"DEBIAN_NETWORK_M5_FAIL reason=",
    b"DEBIAN_BROWSER_WEB_FAIL reason=",
    b"Kernel panic",
    b"Uncaught panic:",
)


def _debug_console_qemu_bootargs() -> str:
    bootargs = qemu_web_network_bootargs(NetworkMode.DIRECT)
    prefix, separator, initargs = bootargs.partition(" -- ")
    if not separator or initargs != "--root-init=systemd":
        raise ValueError("unexpected QEMU web-network bootargs contract")
    prefix = prefix.replace("loglevel=4", "loglevel=off", 1)
    return (
        f"{prefix} "
        "systemd.setenv=ASTERINAS_DESKTOP_M5_NETWORK_MODE=lightweight "
        f"-- {initargs} --debug-console=root"
    )


def classify_debug_console_qemu(
    transcript: bytes, *, expected_debian_release: str, expected_profile: str
) -> GateResult:
    """Require one ordered Stage1-to-root-console lifecycle."""

    if not isinstance(transcript, bytes):
        return GateResult(False, "debug-console transcript must be bytes", None)
    if not expected_debian_release:
        return GateResult(False, "missing expected Debian release", None)
    if expected_profile not in ("desktop-m5-network", "browser-web"):
        return GateResult(False, "unsupported debug-console rootfs profile", None)
    lowered = transcript.lower()
    for marker, reason in _DEBUG_CONSOLE_QEMU_FATAL_MARKERS:
        if marker in lowered:
            return GateResult(False, reason, None)

    positions: list[int] = []
    for milestone in DEBUG_CONSOLE_QEMU_MILESTONES:
        marker = milestone.encode()
        count = transcript.count(marker)
        if count != 1:
            qualifier = "duplicate" if count > 1 else "missing"
            return GateResult(
                False,
                f"{qualifier} debug-console milestone: {milestone}",
                None,
            )
        positions.append(transcript.find(marker))
    if positions != sorted(positions):
        return GateResult(False, "debug-console milestones out of order", None)
    if expected_profile == "browser-web":
        if _M5_DESKTOP_READY in transcript:
            return GateResult(False, "mixed desktop-network acceptance contract", None)
        network = classify_web_network(transcript, mode=NetworkMode.DIRECT)
        if not network.passed:
            return network
        if _WEB_DESKTOP_READY not in transcript:
            return GateResult(False, "missing browser desktop readiness", None)
        return GateResult(True, "pass", None)
    if _WEB_NETWORK_READY in transcript or _WEB_DESKTOP_READY in transcript:
        return GateResult(False, "mixed desktop-network acceptance contract", None)
    return classify_desktop_m5_qemu(
        transcript,
        expected_debian_release=expected_debian_release,
    )


class DebugConsoleQemuOperations(DesktopM5QemuOperations):
    """Add fixed root-shell probes to the established M5 desktop gate."""

    ARTIFACT_PREFIX = "debug-root-console"
    MILESTONES = DEBUG_CONSOLE_QEMU_MILESTONES
    FAILURE_MARKER = b"DEBIAN_ROOTFS_FAIL reason="
    CAPTURE_SCREENSHOT = False
    CAPTURE_DEBUG_SCREENSHOT = True
    BOOTARGS = _debug_console_qemu_bootargs()

    def __init__(self, config: GateConfig) -> None:
        self.debug_evidence: DebugConsoleEvidence | None = None
        self._validated_profile: str | None = None
        super().__init__(config)

    @classmethod
    def _accepted_profile_identities(cls) -> tuple[tuple[int, str], ...]:
        return (
            (5, "desktop-m5-network"),
            (7, "browser-web"),
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = super().validate_inputs(config, snapshots)
        profile = identity.get("profile")
        if not isinstance(profile, str):
            raise GateFailure("validated rootfs identity has no profile")
        self._validated_profile = profile
        return identity

    def classify_transcript(
        self, transcript: bytes, *, expected_debian_release: str
    ) -> GateResult:
        return classify_debug_console_qemu(
            transcript,
            expected_debian_release=expected_debian_release,
            expected_profile=self._validated_profile or "",
        )

    def _wait_for_profile_acceptance(self, serial: Any, deadline: float) -> None:
        if self._validated_profile == "browser-web":
            readiness = (_WEB_DESKTOP_READY, _WEB_NETWORK_READY)
        elif self._validated_profile == "desktop-m5-network":
            readiness = (_M5_DESKTOP_READY,)
        else:
            raise GateFailure("rootfs profile was not validated")
        for marker in readiness:
            completion = serial.wait_for_any(
                (marker, *_POST_CONSOLE_FAILURE_MARKERS), deadline
            )
            if completion in _POST_CONSOLE_FAILURE_MARKERS:
                raise GateFailure("guest failed before desktop-network acceptance")

    def run_protocol(self, session: Mapping[str, Any], config: GateConfig) -> None:
        super().run_protocol(session, config)
        serial = session["serial"]
        self._wait_for_profile_acceptance(
            serial, time.monotonic() + config.boot_timeout
        )
        nonce = secrets.token_hex(16)
        try:
            self.debug_evidence = run_debug_console_phase(
                serial,
                time.monotonic() + config.command_timeout,
                nonce,
                ready_seen=True,
            )
        except (
            DebugConsoleProtocolError,
            TimeoutError,
            BufferError,
            EOFError,
            UnicodeError,
        ) as error:
            raise GateFailure(f"debug-console protocol failed: {error}") from error
        if not self.CAPTURE_DEBUG_SCREENSHOT:
            return
        screenshot = session["directory"] / f"{self.ARTIFACT_PREFIX}.ppm"
        self._screenshot, self._screenshot_metadata = capture_rendered_ppm(
            session["monitor"],
            screenshot,
            time.monotonic() + config.command_timeout,
        )

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        result["debug_console"] = (
            asdict(self.debug_evidence) if self.debug_evidence is not None else None
        )
        super().publish(config, prepared, transcript, result)


def orchestrate_debug_console_qemu_gate(
    config: GateConfig, operations: DebugConsoleQemuOperations
) -> dict[str, object]:
    """Run the one-boot graphical lifecycle with debug-console evidence."""

    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=operations.classify_transcript,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DebugConsoleQemuOperations(config) as operations:
            result = orchestrate_debug_console_qemu_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-debug-console-qemu-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-debug-console-qemu-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
