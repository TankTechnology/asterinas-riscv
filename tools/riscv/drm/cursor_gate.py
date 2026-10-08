#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run and classify the RISC-V virtio-gpu hardware-cursor gate."""

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
from qemu_uboot_devices import DRM_CURSOR
from qemu_uboot_profiles import DRM_CURSOR_READY_LINE
from qemu_uboot_profiles import GENERIC_SV39_DRM_CURSOR_SMP4 as CURSOR_PROFILE
from qemu_uboot_secure_io import PinnedOutputDirectory, PinnedRegularInput


CURSOR_SET_MARKER = b"DRM_CURSOR_SET PASS"
CURSOR_MOVE_MARKER = b"DRM_CURSOR_MOVE PASS"
CURSOR_HIDE_MARKER = b"DRM_CURSOR_HIDE PASS"
CURSOR_READY_MARKER = DRM_CURSOR_READY_LINE
CURSOR_TRACE = b"virtio_gpu_update_cursor"
SET_TRACE_PATTERN = re.compile(
    rb"virtio_gpu_update_cursor scanout 0, x 32, y 24, update, res 0x[1-9a-fA-F][0-9a-fA-F]*"
)
MOVE_TRACE_PATTERN = re.compile(
    rb"virtio_gpu_update_cursor scanout 0, x 96, y 64, move, res 0x0"
)
HIDE_TRACE_PATTERN = re.compile(
    rb"virtio_gpu_update_cursor scanout 0, x 96, y 64, update, res 0x0"
)
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
FATAL_MARKERS = (
    b"Uncaught panic",
    b"unexpected exception",
    b"virtio-gpu cursor update failed",
)


@dataclass(frozen=True)
class CursorGateResult:
    passed: bool
    reason: str
    update_trace_count: int
    move_trace_count: int


@dataclass(frozen=True)
class CursorGateConfig:
    """Immutable inputs and evidence directory for one cursor-gate run."""

    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path


def classify_transcript(transcript: bytes) -> CursorGateResult:
    """Require the exact set/move/hide guest and VirtIO trace sequence."""

    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("cursor transcript exceeds the byte cap")

    trace_lines = tuple(
        line for line in transcript.splitlines() if CURSOR_TRACE in line
    )
    update_count = sum(b", update, res " in line for line in trace_lines)
    move_count = sum(b", move, res " in line for line in trace_lines)
    for marker in FATAL_MARKERS:
        if marker in transcript:
            return CursorGateResult(
                False, f"fatal marker: {marker.decode()}", update_count, move_count
            )

    offset = 0
    for label, pattern in (
        ("cursor update trace", SET_TRACE_PATTERN),
        ("cursor set marker", re.compile(re.escape(CURSOR_SET_MARKER))),
        ("cursor move trace", MOVE_TRACE_PATTERN),
        ("cursor move marker", re.compile(re.escape(CURSOR_MOVE_MARKER))),
        ("cursor hide trace", HIDE_TRACE_PATTERN),
        ("cursor hide marker", re.compile(re.escape(CURSOR_HIDE_MARKER))),
        ("cursor ready marker", re.compile(re.escape(CURSOR_READY_MARKER))),
    ):
        match = pattern.search(transcript, offset)
        if match is None:
            return CursorGateResult(
                False, f"missing or unordered {label}", update_count, move_count
            )
        offset = match.end()

    if len(trace_lines) != 3 or update_count != 2 or move_count != 1:
        return CursorGateResult(
            False, "unexpected cursor trace count", update_count, move_count
        )
    return CursorGateResult(True, "passed", update_count, move_count)


def _read_serial_log(path: Path) -> bytes:
    with (
        PinnedRegularInput.open(path, label="cursor serial log") as serial,
        tempfile.TemporaryDirectory(prefix="asterinas-drm-cursor-") as temporary,
    ):
        copy = Path(temporary) / "serial.log"
        serial.copy_to(copy)
        if copy.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise ValueError("cursor transcript exceeds the byte cap")
        return copy.read_bytes()


def _publish_result(
    output: PinnedOutputDirectory,
    result: CursorGateResult,
) -> None:
    document = json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    with output.atomic_write("result.json", document.encode()):
        output.verify_current()


def run_cursor_gate(
    config: CursorGateConfig,
    *,
    runner: Callable[..., Any] = run_prepared,
) -> CursorGateResult:
    """Run the registered SMP=4 cursor profile and publish final evidence."""

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
                startup_timeout=CURSOR_PROFILE.validation.startup_timeout,
                command_timeout=CURSOR_PROFILE.validation.command_timeout,
                boot_timeout=CURSOR_PROFILE.validation.boot_timeout,
                termination_grace=5.0,
                profile=CURSOR_PROFILE,
                device_set=DRM_CURSOR,
            )
            classified = classify_transcript(
                _read_serial_log(output.path / "serial.log")
            )
            if not bool(base_result.passed):
                result = CursorGateResult(
                    False,
                    "base U-Boot gate failed",
                    classified.update_trace_count,
                    classified.move_trace_count,
                )
            else:
                result = classified
        except Exception as error:
            result = CursorGateResult(
                False,
                f"gate error: {type(error).__name__}: {error}",
                0,
                0,
            )
        _publish_result(output, result)
        return result


def _parse_args(arguments: Sequence[str] | None) -> CursorGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    return CursorGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    result = run_cursor_gate(_parse_args(arguments))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
