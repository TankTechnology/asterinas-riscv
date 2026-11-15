#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run and classify the RISC-V DRM render-node gate."""

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
from qemu_uboot_devices import DRM_RENDER_NODE
from qemu_uboot_profiles import DRM_RENDER_NODE_READY_LINE
from qemu_uboot_profiles import GENERIC_SV39_DRM_RENDER_NODE_SMP4 as RENDER_PROFILE
from qemu_uboot_secure_io import PinnedOutputDirectory, PinnedRegularInput


ALLOWED_MARKER = b"DRM_RENDER_ALLOWED PASS"
REFUSED_MARKER = b"DRM_RENDER_REFUSED PASS"
CARD_MARKER = b"DRM_RENDER_CARD PASS"
READY_MARKER = DRM_RENDER_NODE_READY_LINE
# The probe reports a failed check as `stage=<name>` followed by either the
# check's own detail or the errno the failing syscall set. Both carry the
# diagnosis, so both are kept.
FAIL_PATTERN = re.compile(rb"DRM_RENDER_FAIL stage=([A-Za-z0-9-]+) ([^\r\n]*)")
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
FATAL_MARKERS = (
    b"Uncaught panic",
    b"unexpected exception",
)

# `refused` is the check that gives the gate its meaning; `card` is the control
# that keeps it from passing against a kernel which refuses everything. Both
# have to hold for the split to be real.
STAGE_SEQUENCE = (
    ("render allowed set", re.compile(re.escape(ALLOWED_MARKER))),
    ("render refused set", re.compile(re.escape(REFUSED_MARKER))),
    ("card control", re.compile(re.escape(CARD_MARKER))),
    ("render ready marker", re.compile(re.escape(READY_MARKER))),
)


@dataclass(frozen=True)
class RenderNodeGateResult:
    passed: bool
    reason: str
    stage_count: int
    failed_stage: str


@dataclass(frozen=True)
class RenderNodeGateConfig:
    """Immutable inputs and evidence directory for one render-node-gate run."""

    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path


def classify_transcript(transcript: bytes) -> RenderNodeGateResult:
    """Require the ordered guest markers that prove the node permission split."""

    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("render-node transcript exceeds the byte cap")

    for marker in FATAL_MARKERS:
        if marker in transcript:
            return RenderNodeGateResult(False, f"fatal marker: {marker.decode()}", 0, "")

    # The probe names the ioctl that misbehaved, which a bare "missing marker"
    # would not.
    failure = FAIL_PATTERN.search(transcript)
    if failure is not None:
        stage = failure.group(1).decode()
        trailing = failure.group(2).decode()
        return RenderNodeGateResult(
            False, f"guest reported {stage} failure: {trailing}", 0, stage
        )

    offset = 0
    for index, (label, pattern) in enumerate(STAGE_SEQUENCE):
        match = pattern.search(transcript, offset)
        if match is None:
            return RenderNodeGateResult(False, f"missing or unordered {label}", index, "")
        offset = match.end()

    return RenderNodeGateResult(True, "passed", len(STAGE_SEQUENCE), "")


def _read_serial_log(path: Path) -> bytes:
    with (
        PinnedRegularInput.open(path, label="render-node serial log") as serial,
        tempfile.TemporaryDirectory(prefix="asterinas-drm-render-node-") as temporary,
    ):
        copy = Path(temporary) / "serial.log"
        serial.copy_to(copy)
        if copy.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise ValueError("render-node transcript exceeds the byte cap")
        return copy.read_bytes()


def _publish_result(
    output: PinnedOutputDirectory,
    result: RenderNodeGateResult,
) -> None:
    document = json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    with output.atomic_write("result.json", document.encode()):
        output.verify_current()


def run_render_node_gate(
    config: RenderNodeGateConfig,
    *,
    runner: Callable[..., Any] = run_prepared,
) -> RenderNodeGateResult:
    """Run the registered SMP=4 render-node profile and publish final evidence."""

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
                startup_timeout=RENDER_PROFILE.validation.startup_timeout,
                command_timeout=RENDER_PROFILE.validation.command_timeout,
                boot_timeout=RENDER_PROFILE.validation.boot_timeout,
                termination_grace=5.0,
                profile=RENDER_PROFILE,
                device_set=DRM_RENDER_NODE,
            )
            classified = classify_transcript(
                _read_serial_log(output.path / "serial.log")
            )
            if not bool(base_result.passed):
                result = RenderNodeGateResult(
                    False,
                    "base U-Boot gate failed",
                    classified.stage_count,
                    classified.failed_stage,
                )
            else:
                result = classified
        except Exception as error:
            result = RenderNodeGateResult(
                False,
                f"gate error: {type(error).__name__}: {error}",
                0,
                "",
            )
        _publish_result(output, result)
        return result


def _parse_args(arguments: Sequence[str] | None) -> RenderNodeGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    return RenderNodeGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    result = run_render_node_gate(_parse_args(arguments))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
