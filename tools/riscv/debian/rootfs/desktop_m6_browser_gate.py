#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Classify foreground browser and local JavaScript evidence."""

from __future__ import annotations

import re
import sys
import time
from typing import Any

from tools.riscv.debian.rootfs.desktop_m3_gate import capture_rendered_ppm
from tools.riscv.debian.rootfs.desktop_m4_gate import DESKTOP_M4_MILESTONES
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
    classify_desktop_m5_qemu,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
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


DESKTOP_M6_REMOTE_MARKER = (
    "DEBIAN_BROWSER_M6_REMOTE host=www.baidu.com resource=logo-png foreground=active"
)
DESKTOP_M6_JAVASCRIPT_STATUSES = ("limited-pass", "disabled", "failed")
_JAVASCRIPT_RE = re.compile(
    rb"DEBIAN_BROWSER_M6_JAVASCRIPT status=(limited-pass|disabled|failed)"
)


def classify_desktop_m6_browser(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require one ordered remote page and one classified local JS result."""

    base = classify_desktop_m5_qemu(
        transcript,
        expected_debian_release=expected_debian_release,
    )
    if not base.passed:
        return base
    if b"debian_browser_m6_fail reason=" in transcript.lower():
        return GateResult(False, "browser guest failure", None)

    remote = DESKTOP_M6_REMOTE_MARKER.encode()
    if transcript.count(remote) != 1:
        return GateResult(False, "missing or duplicate remote browser evidence", None)
    javascript_matches = tuple(_JAVASCRIPT_RE.finditer(transcript))
    if len(javascript_matches) != 1:
        return GateResult(False, "missing or duplicate JavaScript evidence", None)

    javascript = javascript_matches[0]
    status = javascript.group(1).decode("ascii")
    ready = f"DEBIAN_BROWSER_M6_READY remote=baidu javascript={status}".encode()
    if transcript.count(ready) != 1:
        return GateResult(False, "missing or mismatched browser ready evidence", None)

    positions = (
        transcript.find(DESKTOP_M4_MILESTONES[-1].encode()),
        transcript.find(remote),
        javascript.start(),
        transcript.find(ready),
    )
    if positions != tuple(sorted(positions)) or len(set(positions)) != len(positions):
        return GateResult(False, "browser milestones out of order", None)
    return GateResult(True, "pass", None)


class DesktopM6BrowserOperations(DesktopM5QemuOperations):
    """Capture remote NetSurf and local JavaScript frames in one M5 boot."""

    ARTIFACT_PREFIX = "desktop-m6-browser"
    MILESTONES = (
        *DESKTOP_M5_QEMU_MILESTONES,
        *DESKTOP_M4_MILESTONES,
        DESKTOP_M6_REMOTE_MARKER,
    )
    FAILURE_MARKER = b"DEBIAN_BROWSER_M6_FAIL reason="
    _qemu_argv = staticmethod(desktop_m5_qemu_argv)

    def __init__(self, config: GateConfig) -> None:
        super().__init__(config)
        self._remote_evidence = False
        self._javascript_status = ""
        self._javascript_screenshot = b""
        self._javascript_screenshot_metadata: dict[str, int] = {}

    def invalidate(self, config: GateConfig) -> None:
        super().invalidate(config)
        self._require_output().invalidate("desktop-m6-javascript.ppm")

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        try:
            super().run_protocol(session, config)
        except GateFailure:
            screenshot = session["directory"] / f"{self.ARTIFACT_PREFIX}.ppm"
            try:
                self._screenshot, self._screenshot_metadata = capture_rendered_ppm(
                    session["monitor"],
                    screenshot,
                    time.monotonic() + config.command_timeout,
                )
            except GateTermination:
                raise
            except Exception:
                pass
            raise
        self._remote_evidence = True
        javascript_markers = tuple(
            f"DEBIAN_BROWSER_M6_JAVASCRIPT status={status}".encode()
            for status in DESKTOP_M6_JAVASCRIPT_STATUSES
        )
        completion = session["serial"].wait_for_any(
            (*javascript_markers, self.FAILURE_MARKER),
            time.monotonic() + config.boot_timeout,
        )
        if completion.startswith(self.FAILURE_MARKER):
            raise GateFailure("guest reported browser failure")
        self._javascript_status = next(
            status
            for status, marker in zip(
                DESKTOP_M6_JAVASCRIPT_STATUSES,
                javascript_markers,
            )
            if completion.startswith(marker)
        )
        ready = (
            f"DEBIAN_BROWSER_M6_READY remote=baidu javascript={self._javascript_status}"
        ).encode()
        session["serial"].wait_for(
            ready,
            time.monotonic() + config.boot_timeout,
        )
        screenshot = session["directory"] / "desktop-m6-javascript.ppm"
        (
            self._javascript_screenshot,
            self._javascript_screenshot_metadata,
        ) = capture_rendered_ppm(
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
        self._require_config(config)
        if self._javascript_screenshot:
            self._require_output().atomic_write(
                "desktop-m6-javascript.ppm",
                self._javascript_screenshot,
                mode=0o600,
            )
        result["javascript_status"] = self._javascript_status
        result["javascript_screenshot"] = self._javascript_screenshot_metadata
        result["remote_evidence"] = self._remote_evidence
        super().publish(config, prepared, transcript, result)


def orchestrate_desktop_m6_browser_gate(
    config: GateConfig, operations: DesktopM6BrowserOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m6_browser,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM6BrowserOperations(config) as operations:
            result = orchestrate_desktop_m6_browser_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m6-browser-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m6-browser-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
