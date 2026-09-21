#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run and classify the RISC-V firmware-framebuffer scanout gate."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from qemu_uboot_booti import run_prepared
from qemu_uboot_devices import DRM_FIRMWARE
from qemu_uboot_profiles import DRM_FIRMWARE_READY_LINE
from qemu_uboot_profiles import GENERIC_SV39_DRM_FIRMWARE_SMP4 as FIRMWARE_PROFILE
from qemu_uboot_secure_io import PinnedOutputDirectory, PinnedRegularInput


DRIVER_MARKER = b"DRM_FIRMWARE_DRIVER PASS"
MAX_MARKER = b"DRM_FIRMWARE_MAX PASS"
PRESENT_MARKER = b"DRM_FIRMWARE_PRESENT PASS"
PATTERN_MARKER = b"DRM_FIRMWARE_PATTERN PASS"
FBDEV_MARKER = b"DRM_FIRMWARE_FBDEV PASS"
READY_MARKER = DRM_FIRMWARE_READY_LINE
FAIL_PATTERN = re.compile(rb"DRM_FIRMWARE_FAIL stage=([A-Za-z0-9-]+) errno=(-?\d+)")
MISMATCH_PATTERN = re.compile(
    rb"DRM_FIRMWARE_MISMATCH x=(\d+) y=(\d+) found=([0-9a-fA-F]+) want=([0-9a-fA-F]+)"
)
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
FATAL_MARKERS = (
    b"Uncaught panic",
    b"unexpected exception",
)

# The order is the claim. Each stage depends on the one before it: the driver
# name decides which backend everything else is testing, the maximum decides
# that the mode this probe is about to set is one the display admits, and the
# pixel comparison is only meaningful once a present has actually happened. A
# transcript with these markers in another order is not a pass with a reordered
# log; it is a different sequence.
STAGE_SEQUENCE = (
    ("firmware driver name", re.compile(re.escape(DRIVER_MARKER))),
    ("fixed maximum mode", re.compile(re.escape(MAX_MARKER))),
    ("displays a frame", re.compile(re.escape(PRESENT_MARKER))),
    ("writes a known pattern", re.compile(re.escape(PATTERN_MARKER))),
    ("fbdev reads the pixels back", re.compile(re.escape(FBDEV_MARKER))),
    ("firmware ready marker", re.compile(re.escape(READY_MARKER))),
)


@dataclass(frozen=True)
class FirmwareGateResult:
    passed: bool
    reason: str
    stage_count: int
    failed_stage: str


@dataclass(frozen=True)
class FirmwareGateConfig:
    """Immutable inputs and evidence directory for one firmware-gate run."""

    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path


def classify_transcript(transcript: bytes) -> FirmwareGateResult:
    """Require the ordered guest sequence that proves firmware scanout."""

    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("firmware transcript exceeds the byte cap")

    for marker in FATAL_MARKERS:
        if marker in transcript:
            return FirmwareGateResult(
                False, f"fatal marker: {marker.decode()}", 0, ""
            )

    # A pixel mismatch is the failure this gate exists to catch, and it is not
    # a stage failure: every ioctl succeeded and the picture is still wrong.
    # Reported before the generic checks so the coordinates survive.
    mismatch = MISMATCH_PATTERN.search(transcript)
    if mismatch is not None:
        x, y, found, want = (group.decode() for group in mismatch.groups())
        return FirmwareGateResult(
            False,
            f"scanout pixel ({x},{y}) is 0x{found} but should be 0x{want}",
            0,
            "pixels",
        )

    # The probe diagnoses its own failure; prefer that over a generic "missing
    # marker", which would not say which stage broke.
    failure = FAIL_PATTERN.search(transcript)
    if failure is not None:
        stage = failure.group(1).decode()
        errno_value = int(failure.group(2))
        return FirmwareGateResult(
            False, f"guest reported {stage} failure: errno {errno_value}", 0, stage
        )

    offset = 0
    for index, (label, pattern) in enumerate(STAGE_SEQUENCE):
        match = pattern.search(transcript, offset)
        if match is None:
            return FirmwareGateResult(False, f"missing or unordered {label}", index, "")
        offset = match.end()

    return FirmwareGateResult(True, "passed", len(STAGE_SEQUENCE), "")


def _read_serial_log(path: Path) -> bytes:
    with (
        PinnedRegularInput.open(path, label="firmware serial log") as serial,
        tempfile.TemporaryDirectory(prefix="asterinas-drm-firmware-") as temporary,
    ):
        copy = Path(temporary) / "serial.log"
        serial.copy_to(copy)
        if copy.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise ValueError("firmware transcript exceeds the byte cap")
        return copy.read_bytes()


def _publish_result(
    output: PinnedOutputDirectory,
    result: FirmwareGateResult,
) -> None:
    document = json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    with output.atomic_write("result.json", document.encode()):
        output.verify_current()


def run_firmware_gate(
    config: FirmwareGateConfig,
    *,
    runner: Callable[..., Any] = run_prepared,
) -> FirmwareGateResult:
    """Run the registered SMP=4 firmware profile and publish final evidence."""

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
                startup_timeout=FIRMWARE_PROFILE.validation.startup_timeout,
                command_timeout=FIRMWARE_PROFILE.validation.command_timeout,
                boot_timeout=FIRMWARE_PROFILE.validation.boot_timeout,
                termination_grace=5.0,
                profile=FIRMWARE_PROFILE,
                device_set=DRM_FIRMWARE,
                # A framebuffer device set is defined by being screenshottable,
                # and the runner holds it to that: asking for a bochs display
                # without asking for the picture it produces is refused. The
                # capture is the runner's, from QEMU's monitor; the probe's own
                # fbdev read-back is the claim this gate actually grades.
                screenshot=output.path / "framebuffer.ppm",
                display_audit=output.path / "display-audit.json",
            )
            classified = classify_transcript(
                _read_serial_log(output.path / "serial.log")
            )
            if not bool(base_result.passed):
                result = FirmwareGateResult(
                    False,
                    "base U-Boot gate failed",
                    classified.stage_count,
                    classified.failed_stage,
                )
            else:
                result = classified
        except Exception as error:
            result = FirmwareGateResult(
                False,
                f"gate error: {type(error).__name__}: {error}",
                0,
                "",
            )
        _publish_result(output, result)
        return result


def _parse_args(arguments: Sequence[str] | None) -> FirmwareGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    return FirmwareGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    result = run_firmware_gate(_parse_args(arguments))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
