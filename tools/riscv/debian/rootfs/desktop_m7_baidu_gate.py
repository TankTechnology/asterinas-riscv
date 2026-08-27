#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Capture a real Baidu homepage and basic search in Debian NetSurf."""

from __future__ import annotations

import sys
import time
from typing import Any

from tools.riscv.debian.rootfs.desktop_m3_gate import capture_rendered_ppm
from tools.riscv.debian.rootfs.desktop_m6_browser_gate import (
    DesktopM6BrowserOperations,
    classify_desktop_m6_browser,
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


DESKTOP_M7_HOME_MARKER = (
    "DEBIAN_BROWSER_M7_HOME url=https://www.baidu.com/ title=baidu process=netsurf"
)
DESKTOP_M7_SEARCH_MARKER = (
    "DEBIAN_BROWSER_M7_SEARCH query=asterinas-riscv result=loaded"
)
DESKTOP_M7_READY_MARKER = "DEBIAN_BROWSER_M7_READY page=baidu search=pass"
DESKTOP_M7_FAILURE_MARKER = b"DEBIAN_BROWSER_M7_FAIL reason="


def classify_desktop_m7_baidu(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require exact ordered M6, homepage, search, and final evidence."""

    base = classify_desktop_m6_browser(
        transcript,
        expected_debian_release=expected_debian_release,
    )
    if not base.passed:
        return base
    if DESKTOP_M7_FAILURE_MARKER.lower() in transcript.lower():
        return GateResult(False, "Baidu page guest failure", None)

    markers = (
        DESKTOP_M7_HOME_MARKER.encode(),
        DESKTOP_M7_SEARCH_MARKER.encode(),
        DESKTOP_M7_READY_MARKER.encode(),
    )
    if any(transcript.count(marker) != 1 for marker in markers):
        return GateResult(False, "missing or duplicate Baidu page evidence", None)
    positions = tuple(transcript.find(marker) for marker in markers)
    if positions != tuple(sorted(positions)) or len(set(positions)) != len(positions):
        return GateResult(False, "Baidu page milestones out of order", None)
    return GateResult(True, "pass", None)


class DesktopM7BaiduOperations(DesktopM6BrowserOperations):
    """Capture homepage and search frames after the complete M6 baseline."""

    ARTIFACT_PREFIX = "desktop-m7-baidu"

    def __init__(self, config: GateConfig) -> None:
        super().__init__(config)
        self._home_screenshot = b""
        self._home_screenshot_metadata: dict[str, int] = {}
        self._search_screenshot = b""
        self._search_screenshot_metadata: dict[str, int] = {}
        self._failure_screenshot = b""
        self._failure_screenshot_metadata: dict[str, int] = {}

    def invalidate(self, config: GateConfig) -> None:
        super().invalidate(config)
        self._require_output().invalidate(
            "desktop-m7-baidu-home.ppm",
            "desktop-m7-baidu-search.ppm",
            "desktop-m7-baidu-failure.ppm",
        )

    @staticmethod
    def _wait_marker(
        session: dict[str, Any], marker: bytes, config: GateConfig
    ) -> None:
        observed = session["serial"].wait_for_any(
            (marker, DESKTOP_M7_FAILURE_MARKER),
            time.monotonic() + config.boot_timeout,
        )
        if observed.startswith(DESKTOP_M7_FAILURE_MARKER):
            raise GateFailure("guest reported Baidu page failure")

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        super().run_protocol(session, config)
        try:
            self._wait_marker(session, DESKTOP_M7_HOME_MARKER.encode(), config)
            (
                self._home_screenshot,
                self._home_screenshot_metadata,
            ) = capture_rendered_ppm(
                session["monitor"],
                session["directory"] / "desktop-m7-baidu-home.ppm",
                time.monotonic() + config.command_timeout,
            )
            self._wait_marker(session, DESKTOP_M7_SEARCH_MARKER.encode(), config)
            session["serial"].wait_for(
                DESKTOP_M7_READY_MARKER.encode(),
                time.monotonic() + config.boot_timeout,
            )
            (
                self._search_screenshot,
                self._search_screenshot_metadata,
            ) = capture_rendered_ppm(
                session["monitor"],
                session["directory"] / "desktop-m7-baidu-search.ppm",
                time.monotonic() + config.command_timeout,
            )
        except GateFailure:
            try:
                (
                    self._failure_screenshot,
                    self._failure_screenshot_metadata,
                ) = capture_rendered_ppm(
                    session["monitor"],
                    session["directory"] / "desktop-m7-baidu-failure.ppm",
                    time.monotonic() + config.command_timeout,
                )
            except GateTermination:
                raise
            except Exception:
                pass
            raise

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        self._require_config(config)
        output = self._require_output()
        if self._home_screenshot:
            output.atomic_write(
                "desktop-m7-baidu-home.ppm",
                self._home_screenshot,
                mode=0o600,
            )
        if self._search_screenshot:
            output.atomic_write(
                "desktop-m7-baidu-search.ppm",
                self._search_screenshot,
                mode=0o600,
            )
        if self._failure_screenshot:
            output.atomic_write(
                "desktop-m7-baidu-failure.ppm",
                self._failure_screenshot,
                mode=0o600,
            )
        result["homepage_screenshot"] = self._home_screenshot_metadata
        result["search_screenshot"] = self._search_screenshot_metadata
        result["failure_screenshot"] = self._failure_screenshot_metadata
        super().publish(config, prepared, transcript, result)


def orchestrate_desktop_m7_baidu_gate(
    config: GateConfig, operations: DesktopM7BaiduOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m7_baidu,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopM7BaiduOperations(config) as operations:
            result = orchestrate_desktop_m7_baidu_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-desktop-m7-baidu-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-m7-baidu-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
