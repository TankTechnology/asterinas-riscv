#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run and classify the RISC-V GEM object-space gate."""

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
from qemu_uboot_devices import DRM_GEM
from qemu_uboot_profiles import DRM_GEM_READY_LINE
from qemu_uboot_profiles import GENERIC_SV39_DRM_GEM_SMP4 as GEM_PROFILE
from qemu_uboot_secure_io import PinnedOutputDirectory, PinnedRegularInput


CREATE_MARKER = b"DRM_GEM_CREATE PASS"
FLINK_MARKER = b"DRM_GEM_FLINK PASS"
OPEN_MARKER = b"DRM_GEM_OPEN PASS"
CLOSE_MARKER = b"DRM_GEM_CLOSE PASS"
REJECT_MARKER = b"DRM_GEM_REJECT PASS"
READY_MARKER = DRM_GEM_READY_LINE
FAIL_PATTERN = re.compile(rb"DRM_GEM_FAIL stage=([A-Za-z0-9-]+) errno=(-?\d+)")
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
FATAL_MARKERS = (
    b"Uncaught panic",
    b"unexpected exception",
)

# The probe only earns a pass by walking the whole sequence in order: each stage
# depends on the object the previous one produced, so a reordered or partial
# transcript means the ioctl family is not behaving as one object space.
STAGE_SEQUENCE = (
    ("GEM create", re.compile(re.escape(CREATE_MARKER))),
    ("GEM flink", re.compile(re.escape(FLINK_MARKER))),
    ("GEM open", re.compile(re.escape(OPEN_MARKER))),
    ("GEM close", re.compile(re.escape(CLOSE_MARKER))),
    ("GEM reject", re.compile(re.escape(REJECT_MARKER))),
    ("GEM ready marker", re.compile(re.escape(READY_MARKER))),
)


@dataclass(frozen=True)
class GemGateResult:
    passed: bool
    reason: str
    stage_count: int
    failed_stage: str


@dataclass(frozen=True)
class GemGateConfig:
    """Immutable inputs and evidence directory for one GEM-gate run."""

    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path


def classify_transcript(transcript: bytes) -> GemGateResult:
    """Require the ordered guest sequence that proves one GEM object space."""

    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("GEM transcript exceeds the byte cap")

    for marker in FATAL_MARKERS:
        if marker in transcript:
            return GemGateResult(False, f"fatal marker: {marker.decode()}", 0, "")

    # The probe diagnoses its own failure; prefer that over a generic
    # "missing marker", which would not say which ioctl broke.
    failure = FAIL_PATTERN.search(transcript)
    if failure is not None:
        stage = failure.group(1).decode()
        errno_value = int(failure.group(2))
        return GemGateResult(
            False, f"guest reported {stage} failure: errno {errno_value}", 0, stage
        )

    offset = 0
    for index, (label, pattern) in enumerate(STAGE_SEQUENCE):
        match = pattern.search(transcript, offset)
        if match is None:
            return GemGateResult(False, f"missing or unordered {label}", index, "")
        offset = match.end()

    return GemGateResult(True, "passed", len(STAGE_SEQUENCE), "")


def _read_serial_log(path: Path) -> bytes:
    with (
        PinnedRegularInput.open(path, label="GEM serial log") as serial,
        tempfile.TemporaryDirectory(prefix="asterinas-drm-gem-") as temporary,
    ):
        copy = Path(temporary) / "serial.log"
        serial.copy_to(copy)
        if copy.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise ValueError("GEM transcript exceeds the byte cap")
        return copy.read_bytes()


def _publish_result(
    output: PinnedOutputDirectory,
    result: GemGateResult,
) -> None:
    document = json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    with output.atomic_write("result.json", document.encode()):
        output.verify_current()


def run_gem_gate(
    config: GemGateConfig,
    *,
    runner: Callable[..., Any] = run_prepared,
) -> GemGateResult:
    """Run the registered SMP=4 GEM profile and publish final evidence."""

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
                startup_timeout=GEM_PROFILE.validation.startup_timeout,
                command_timeout=GEM_PROFILE.validation.command_timeout,
                boot_timeout=GEM_PROFILE.validation.boot_timeout,
                termination_grace=5.0,
                profile=GEM_PROFILE,
                device_set=DRM_GEM,
            )
            classified = classify_transcript(
                _read_serial_log(output.path / "serial.log")
            )
            if not bool(base_result.passed):
                result = GemGateResult(
                    False,
                    "base U-Boot gate failed",
                    classified.stage_count,
                    classified.failed_stage,
                )
            else:
                result = classified
        except Exception as error:
            result = GemGateResult(
                False,
                f"gate error: {type(error).__name__}: {error}",
                0,
                "",
            )
        _publish_result(output, result)
        return result


def _parse_args(arguments: Sequence[str] | None) -> GemGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    return GemGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    result = run_gem_gate(_parse_args(arguments))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
