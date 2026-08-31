#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Gate deterministic lightweight NetSurf interactions after the M7 baseline."""

from __future__ import annotations

import hashlib
import re
import sys
import time
from collections.abc import Mapping
from typing import Any

from tools.riscv.debian.rootfs.desktop_m3_gate import capture_rendered_ppm
from tools.riscv.debian.rootfs.desktop_m7_baidu_gate import (
    DESKTOP_M7_READY_MARKER,
    DesktopM7BaiduOperations,
    classify_desktop_m7_baidu,
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
from tools.riscv.megrez_network_fixture import BROWSER_CAPTURE_PATH


DESKTOP_M8_FIXTURE_MARKER = (
    "DEBIAN_BROWSER_M8_FIXTURE text=cjk-latin image=png form=query"
)
DESKTOP_M8_SCROLL_MARKER = "DEBIAN_BROWSER_M8_SCROLL direction=end-home"
DESKTOP_M8_NAVIGATION_MARKER = (
    "DEBIAN_BROWSER_M8_NAVIGATION second=loaded back=loaded forward=loaded"
)
DESKTOP_M8_DOWNLOAD_MARKER = (
    "DEBIAN_BROWSER_M8_DOWNLOAD bytes=262144 "
    "sha256=2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9"
)
DESKTOP_M8_SOAK_MARKER = "DEBIAN_BROWSER_M8_SOAK seconds=120 process=alive"
DESKTOP_M8_CAPTURE_PREFIX = "DEBIAN_BROWSER_M8_CAPTURE bytes="
DESKTOP_M8_READY_MARKER = "DEBIAN_BROWSER_M8_READY quality=lightweight"
DESKTOP_M8_FAILURE_MARKER = b"DEBIAN_BROWSER_M8_FAIL reason="
DESKTOP_M8_FIXED_MILESTONES = tuple(
    marker.encode()
    for marker in (
        DESKTOP_M8_FIXTURE_MARKER,
        DESKTOP_M8_SCROLL_MARKER,
        DESKTOP_M8_NAVIGATION_MARKER,
        DESKTOP_M8_DOWNLOAD_MARKER,
        DESKTOP_M8_SOAK_MARKER,
    )
)
DESKTOP_M8_CAPTURE_PATTERN = re.compile(
    rb"DEBIAN_BROWSER_M8_CAPTURE bytes=([1-9][0-9]{0,7}) "
    rb"sha256=([0-9a-f]{64})(?:\r*\n|$)"
)
DESKTOP_M8_BROWSER_QUALITY_MILESTONES = (
    DESKTOP_M8_FIXTURE_MARKER,
    DESKTOP_M8_SCROLL_MARKER,
    DESKTOP_M8_NAVIGATION_MARKER,
    DESKTOP_M8_DOWNLOAD_MARKER,
    DESKTOP_M8_SOAK_MARKER,
    DESKTOP_M8_CAPTURE_PREFIX,
    DESKTOP_M8_READY_MARKER,
)
QUALITY_CAPTURE_NAMES = (
    "desktop-m8-fixture.ppm",
    "desktop-m8-navigation.ppm",
    "desktop-m8-download.ppm",
    "desktop-m8-final.ppm",
)
FAILURE_CAPTURE_NAME = "desktop-m8-failure.ppm"


def classify_desktop_m8_browser_quality(
    transcript: bytes,
    *,
    expected_debian_release: str,
) -> GateResult:
    """Require one complete M7 transcript followed by ordered M8 evidence."""

    base = classify_desktop_m7_baidu(
        transcript,
        expected_debian_release=expected_debian_release,
    )
    if not base.passed:
        return base
    if DESKTOP_M8_FAILURE_MARKER.lower() in transcript.lower():
        return GateResult(False, "browser quality guest failure", None)

    ready = DESKTOP_M8_READY_MARKER.encode()
    markers = (*DESKTOP_M8_FIXED_MILESTONES, ready)
    if any(transcript.count(marker) != 1 for marker in markers):
        return GateResult(
            False,
            "missing or duplicate browser quality evidence",
            None,
        )
    captures = tuple(DESKTOP_M8_CAPTURE_PATTERN.finditer(transcript))
    if len(captures) != 1 or transcript.count(DESKTOP_M8_CAPTURE_PREFIX.encode()) != 1:
        return GateResult(False, "missing or duplicate browser capture", None)
    positions = (
        transcript.find(DESKTOP_M7_READY_MARKER.encode()),
        *(transcript.find(marker) for marker in DESKTOP_M8_FIXED_MILESTONES),
        captures[0].start(),
        transcript.find(ready),
    )
    if positions != tuple(sorted(positions)) or len(set(positions)) != len(positions):
        return GateResult(False, "browser quality milestones out of order", None)
    return GateResult(True, "pass", None)


def _capture_marker(summary: Mapping[str, object]) -> bytes | None:
    size = summary.get("bytes")
    digest = summary.get("sha256")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= 8 * 1024 * 1024
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        return None
    return f"DEBIAN_BROWSER_M8_CAPTURE bytes={size} sha256={digest}".encode()


class DesktopM8BrowserQualityOperations(DesktopM7BaiduOperations):
    """Capture four deterministic quality states after the complete M7 gate."""

    ARTIFACT_PREFIX = "desktop-m8-browser-quality"

    def __init__(self, config: GateConfig) -> None:
        super().__init__(config)
        self._quality_screenshots: dict[str, tuple[bytes, dict[str, int]]] = {
            name: (b"", {}) for name in QUALITY_CAPTURE_NAMES
        }
        self._quality_failure_screenshot = b""
        self._quality_failure_screenshot_metadata: dict[str, int] = {}

    def invalidate(self, config: GateConfig) -> None:
        super().invalidate(config)
        self._require_output().invalidate(
            *QUALITY_CAPTURE_NAMES,
            FAILURE_CAPTURE_NAME,
        )

    @staticmethod
    def _wait_m8_marker(
        session: dict[str, Any],
        marker: bytes,
        config: GateConfig,
        start: int,
    ) -> None:
        observed = session["serial"].wait_for_any(
            (marker, DESKTOP_M8_FAILURE_MARKER),
            time.monotonic() + config.boot_timeout,
            start=start,
        )
        if observed.startswith(DESKTOP_M8_FAILURE_MARKER):
            raise GateFailure("guest reported browser quality failure")

    def _capture(
        self,
        session: dict[str, Any],
        config: GateConfig,
        name: str,
    ) -> None:
        self._quality_screenshots[name] = capture_rendered_ppm(
            session["monitor"],
            session["directory"] / name,
            time.monotonic() + config.command_timeout,
        )

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        super().run_protocol(session, config)
        start = session["serial"].checkpoint()
        try:
            self._wait_m8_marker(session, DESKTOP_M8_FIXED_MILESTONES[0], config, start)
            self._capture(session, config, QUALITY_CAPTURE_NAMES[0])
            self._wait_m8_marker(session, DESKTOP_M8_FIXED_MILESTONES[1], config, start)
            self._wait_m8_marker(session, DESKTOP_M8_FIXED_MILESTONES[2], config, start)
            self._capture(session, config, QUALITY_CAPTURE_NAMES[1])
            self._wait_m8_marker(session, DESKTOP_M8_FIXED_MILESTONES[3], config, start)
            self._capture(session, config, QUALITY_CAPTURE_NAMES[2])
            self._wait_m8_marker(session, DESKTOP_M8_FIXED_MILESTONES[4], config, start)
            self._wait_m8_marker(
                session,
                DESKTOP_M8_CAPTURE_PREFIX.encode(),
                config,
                start,
            )
            self._wait_m8_marker(
                session,
                DESKTOP_M8_READY_MARKER.encode(),
                config,
                start,
            )
            self._capture(session, config, QUALITY_CAPTURE_NAMES[3])
        except GateFailure:
            try:
                (
                    self._quality_failure_screenshot,
                    self._quality_failure_screenshot_metadata,
                ) = capture_rendered_ppm(
                    session["monitor"],
                    session["directory"] / FAILURE_CAPTURE_NAME,
                    time.monotonic() + config.command_timeout,
                )
            except GateTermination:
                raise
            except Exception:
                pass
            raise

    def _validate_capture_evidence(
        self,
        transcript: bytes,
    ) -> tuple[bool, str]:
        summary = self.fixture.capture_summary()
        payload = self.fixture.capture_payload()
        if summary is None or payload is None:
            return False, "browser capture missing"
        marker = _capture_marker(summary)
        if (
            marker is None
            or summary.get("path") != BROWSER_CAPTURE_PATH
            or summary.get("peer") != "127.0.0.1"
            or summary.get("bytes") != len(payload)
            or summary.get("sha256") != hashlib.sha256(payload).hexdigest()
            or transcript.count(marker) != 1
        ):
            return False, "browser capture evidence mismatch"
        return True, "pass"

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        self._require_config(config)
        output = self._require_output()
        metadata: dict[str, dict[str, int]] = {}
        for name in QUALITY_CAPTURE_NAMES:
            payload, details = self._quality_screenshots[name]
            metadata[name] = details
            if payload:
                output.atomic_write(name, payload, mode=0o600)
        if self._quality_failure_screenshot:
            output.atomic_write(
                FAILURE_CAPTURE_NAME,
                self._quality_failure_screenshot,
                mode=0o600,
            )
        result["screenshots"] = metadata
        result["quality_failure_screenshot"] = self._quality_failure_screenshot_metadata
        if result.get("passed") is True:
            valid, reason = self._validate_capture_evidence(transcript)
            if not valid:
                result["passed"] = False
                result["reason"] = reason
        super().publish(config, prepared, transcript, result)


def orchestrate_desktop_m8_browser_quality_gate(
    config: GateConfig,
    operations: DesktopM8BrowserQualityOperations,
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m8_browser_quality,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with (
            TerminationSignalState(),
            DesktopM8BrowserQualityOperations(config) as operations,
        ):
            result = orchestrate_desktop_m8_browser_quality_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            "debian-desktop-m8-browser-quality-gate: "
            f"terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(
            f"debian-desktop-m8-browser-quality-gate: {reason}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
