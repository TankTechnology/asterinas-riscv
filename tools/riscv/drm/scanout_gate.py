#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run and classify the independent virtio-gpu DRM scanout gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from qemu_uboot_booti import run_prepared
from qemu_uboot_devices import DRM_SCANOUT, DRM_SCANOUT_GTK, QemuDeviceSet, device_set_by_name
from qemu_uboot_profiles import DRM_SCANOUT_READY_LINE
from qemu_uboot_profiles import GENERIC_SV39_DRM_SCANOUT_SMP4 as SCANOUT_PROFILE
from qemu_uboot_secure_io import PinnedOutputDirectory, PinnedRegularInput


SCANOUT_DEVICE_MARKER = b"DRM_SCANOUT_DEVICE virtio-gpu"
# This is the Linux DRM driver-name ABI, not QEMU's device model name.  The
# underscore is required for Mesa's non-PCI virtio loader; accepting the
# hyphen would let the gate pass while Mesa silently selects kms_swrast.
SCANOUT_VERSION_MARKER = b"DRM_SCANOUT_VERSION name=virtio_gpu"
SCANOUT_CAP_MARKER = b"DRM_SCANOUT_CAP dumb=1"
SCANOUT_RESOURCES_MARKER = b"DRM_SCANOUT_RESOURCES crtcs=1 connectors=1 encoders=1"
SCANOUT_CONNECTOR_MARKER = b"DRM_SCANOUT_CONNECTOR connected=1 modes=1"
SCANOUT_DUMB_MARKER = b"DRM_SCANOUT_DUMB create=mode bpp=32"
SCANOUT_READBACK_MARKER = b"DRM_SCANOUT_READBACK status=pass"
SCANOUT_ADDFB_MARKER = b"DRM_SCANOUT_ADDFB status=pass"
SCANOUT_SETCRTC_MARKER = b"DRM_SCANOUT_SETCRTC status=pass"
SCANOUT_GETCRTC_MARKER = b"DRM_SCANOUT_GETCRTC fb=1"
SCANOUT_CLEANUP_MARKER = b"DRM_SCANOUT_CLEANUP status=pass"
SCANOUT_READY_MARKER = DRM_SCANOUT_READY_LINE

TRACE_PATTERNS = (
    ("create", re.compile(rb"virtio_gpu_cmd_res_create_2d .*w [1-9][0-9]*, h [1-9][0-9]*")),
    ("attach", re.compile(rb"virtio_gpu_cmd_res_back_attach res 0x[1-9a-fA-F][0-9a-fA-F]*")),
    ("setcrtc", re.compile(rb"virtio_gpu_cmd_set_scanout id 0, res 0x[1-9a-fA-F][0-9a-fA-F]*, w [1-9][0-9]*, h [1-9][0-9]*")),
    ("transfer", re.compile(rb"virtio_gpu_cmd_res_xfer_toh_2d res 0x[1-9a-fA-F][0-9a-fA-F]*")),
    ("flush", re.compile(rb"virtio_gpu_cmd_res_flush res 0x[1-9a-fA-F][0-9a-fA-F]*, w [1-9][0-9]*, h [1-9][0-9]*")),
)
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
FATAL_MARKERS = (
    b"Uncaught panic",
    b"unexpected exception",
    b"DRM_SCANOUT_FAIL",
    b"virtio-gpu present failed",
)
FORBIDDEN_FALLBACK_MARKERS = (b"DEBIAN_DESKTOP_M4_", b"DRM_CURSOR_")


@dataclass(frozen=True)
class ScanoutGateResult:
    passed: bool
    reason: str
    trace_count: int
    screenshot_sha256: str | None = None
    screenshot_audit: dict[str, object] | None = None
    device_set: str = DRM_SCANOUT.name
    display_backend: str = DRM_SCANOUT.display


@dataclass(frozen=True)
class ScanoutGateConfig:
    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path
    device_set: str = DRM_SCANOUT.name


def classify_transcript(transcript: bytes) -> ScanoutGateResult:
    """Require DRM/KMS markers and the matching ordered VirtIO control trace."""

    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("DRM scanout transcript exceeds the byte cap")
    for marker in FATAL_MARKERS:
        if marker in transcript:
            return ScanoutGateResult(False, f"fatal marker: {marker.decode()}", 0)
    for marker in FORBIDDEN_FALLBACK_MARKERS:
        if marker in transcript:
            return ScanoutGateResult(False, f"fallback marker: {marker.decode()}", 0)

    offset = 0
    markers = (
        ("device", SCANOUT_DEVICE_MARKER),
        ("version", SCANOUT_VERSION_MARKER),
        ("capability", SCANOUT_CAP_MARKER),
        ("resources", SCANOUT_RESOURCES_MARKER),
        ("connector", SCANOUT_CONNECTOR_MARKER),
        ("dumb buffer", SCANOUT_DUMB_MARKER),
        ("readback", SCANOUT_READBACK_MARKER),
        ("addfb", SCANOUT_ADDFB_MARKER),
        ("setcrtc", SCANOUT_SETCRTC_MARKER),
        ("getcrtc", SCANOUT_GETCRTC_MARKER),
        ("cleanup", SCANOUT_CLEANUP_MARKER),
        ("ready", SCANOUT_READY_MARKER),
    )
    for label, marker in markers:
        position = transcript.find(marker, offset)
        if position < 0:
            return ScanoutGateResult(False, f"missing or unordered {label}", 0)
        offset = position + len(marker)

    trace_start = transcript.find(SCANOUT_DEVICE_MARKER) + len(SCANOUT_DEVICE_MARKER)
    trace_window = transcript[trace_start:]
    trace_lines = tuple(
        line for line in trace_window.splitlines() if b"virtio_gpu_cmd_" in line
    )
    trace_count = 0
    trace_offset = 0
    for label, pattern in TRACE_PATTERNS:
        match = pattern.search(trace_window, trace_offset)
        if match is None:
            return ScanoutGateResult(
                False, f"missing or unordered QEMU scanout {label} trace", trace_count
            )
        trace_count += 1
        trace_offset = match.end()
    if len(trace_lines) != len(TRACE_PATTERNS):
        return ScanoutGateResult(False, "unexpected QEMU scanout trace count", trace_count)
    return ScanoutGateResult(True, "passed", trace_count)


def _read_serial_log(path: Path) -> bytes:
    with (
        PinnedRegularInput.open(path, label="DRM scanout serial log") as serial,
        tempfile.TemporaryDirectory(prefix="asterinas-drm-scanout-") as temporary,
    ):
        copy = Path(temporary) / "serial.log"
        serial.copy_to(copy)
        if copy.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise ValueError("DRM scanout transcript exceeds the byte cap")
        return copy.read_bytes()


def _screenshot_evidence(
    output: PinnedOutputDirectory,
) -> tuple[str | None, dict[str, object] | None]:
    screenshot = output.path / "scanout.ppm"
    audit = output.path / "display-audit.json"
    if not screenshot.exists():
        return None, None
    if screenshot.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise ValueError("DRM scanout screenshot exceeds the byte cap")
    payload = screenshot.read_bytes()
    metadata: dict[str, object] | None = None
    if audit.exists():
        value = json.loads(audit.read_text())
        if not isinstance(value, dict):
            raise ValueError("DRM scanout display audit is not an object")
        metadata = value
    return hashlib.sha256(payload).hexdigest(), metadata


def _publish_result(output: PinnedOutputDirectory, result: ScanoutGateResult) -> None:
    document = json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    with output.atomic_write("result.json", document.encode()):
        output.verify_current()


def run_scanout_gate(
    config: ScanoutGateConfig,
    *,
    runner: Callable[..., Any] = run_prepared,
    device_set: QemuDeviceSet = DRM_SCANOUT,
) -> ScanoutGateResult:
    """Run the registered DRM scanout profile and publish final evidence."""

    with PinnedOutputDirectory.open(config.output_directory) as output:
        output.remove_entry("result.json")
        output.sync()
        output.verify_current()
        try:
            base_result = runner(
                uboot=config.uboot,
                boot_disk=config.boot_disk,
                manifest=config.manifest,
                serial_log=output.path / "serial.log",
                marker_event=output.path / "marker-event.txt",
                result_path=output.path / "boot-result.json",
                screenshot=output.path / "scanout.ppm",
                display_audit=output.path / "display-audit.json",
                startup_timeout=SCANOUT_PROFILE.validation.startup_timeout,
                command_timeout=SCANOUT_PROFILE.validation.command_timeout,
                boot_timeout=SCANOUT_PROFILE.validation.boot_timeout,
                termination_grace=5.0,
                profile=SCANOUT_PROFILE,
                device_set=device_set,
            )
            classified = classify_transcript(_read_serial_log(output.path / "serial.log"))
            screenshot_sha256, screenshot_audit = _screenshot_evidence(output)
            if not bool(base_result.passed):
                result = ScanoutGateResult(
                    False,
                    "base U-Boot gate failed",
                    classified.trace_count,
                    screenshot_sha256,
                    screenshot_audit,
                    device_set.name,
                    device_set.display,
                )
            else:
                result = ScanoutGateResult(
                    classified.passed,
                    classified.reason,
                    classified.trace_count,
                    screenshot_sha256,
                    screenshot_audit,
                    device_set.name,
                    device_set.display,
                )
        except Exception as error:
            result = ScanoutGateResult(
                False,
                f"gate error: {type(error).__name__}: {error}",
                0,
                device_set=device_set.name,
                display_backend=device_set.display,
            )
        _publish_result(output, result)
        return result


def _parse_args(arguments: Sequence[str] | None) -> ScanoutGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument(
        "--device-set",
        choices=(DRM_SCANOUT.name, DRM_SCANOUT_GTK.name),
        default=DRM_SCANOUT.name,
    )
    parsed = parser.parse_args(arguments)
    return ScanoutGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
        device_set=parsed.device_set,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parse_args(arguments)
    # Keep the CLI mapping closed; the dataclass remains intentionally small so
    # programmatic callers retain the original default behavior.
    device_set = device_set_by_name(parsed.device_set)
    result = run_scanout_gate(parsed, device_set=device_set)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
