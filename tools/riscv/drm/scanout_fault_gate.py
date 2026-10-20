#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run one recoverable 2D virtio-gpu scanout fault contract under QEMU."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from qemu_uboot_booti import run_prepared
from qemu_uboot_devices import DRM_SCANOUT_FAULT
from qemu_uboot_profiles import profile_by_name
from qemu_uboot_secure_io import PinnedOutputDirectory


FAULT_PROFILES = {
    "setcrtc": "generic-sv39-drm-scanout-fault-setcrtc-smp4",
    "transfer": "generic-sv39-drm-scanout-fault-transfer-smp4",
    "flush": "generic-sv39-drm-scanout-fault-flush-smp4",
}
FAIL_PATTERN = re.compile(
    rb"DRM_SCANOUT_FAIL stage=([A-Za-z0-9-]+) errno=(-?\d+)"
)
# QEMU's serial backend records CRLF while fixture tests generally use LF.
FAULT_PATTERN = re.compile(
    rb"^DRM_SCANOUT_FAULT stage=([A-Za-z0-9-]+)\r?$", re.MULTILINE
)
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ScanoutFaultGateResult:
    passed: bool
    reason: str
    requested_fault: str
    reported_stage: str | None
    recovery_complete: bool
    serial_sha256: str | None = None


def classify_fault_transcript(
    transcript: bytes, *, requested_fault: str, expected_stage: str
) -> ScanoutFaultGateResult:
    """Require one stage-specific guest failure and no clean ready marker."""

    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("scanout fault transcript exceeds the byte cap")
    if b"Uncaught panic" in transcript or b"unexpected exception" in transcript:
        return ScanoutFaultGateResult(
            False, "fatal marker in fault transcript", requested_fault, None, False
        )
    failure = FAIL_PATTERN.search(transcript)
    if failure is None:
        return ScanoutFaultGateResult(
            False, "missing guest failure diagnosis", requested_fault, None, False
        )
    outer_stage = failure.group(1).decode()
    # The DRM_MODE_SETCRTC ioctl performs the whole virtio-gpu 2D sequence.
    # Therefore transfer/flush faults are reported by userspace at the outer
    # ioctl boundary as `setcrtc`; the kernel marker below identifies the exact
    # inner control-queue operation.
    if outer_stage not in {"setcrtc", expected_stage}:
        return ScanoutFaultGateResult(
            False,
            f"guest reported outer stage {outer_stage} instead of setcrtc/{expected_stage}",
            requested_fault,
            outer_stage,
            False,
        )
    markers = FAULT_PATTERN.findall(transcript)
    if markers != [expected_stage.encode()]:
        return ScanoutFaultGateResult(
            False,
            f"expected one stable fault marker, got {len(markers)}",
            requested_fault,
            expected_stage,
            False,
        )
    if b"ASTERINAS_DRM_SCANOUT_R1_READY" in transcript:
        return ScanoutFaultGateResult(
            False,
            "fault run reached the success marker",
            requested_fault,
            expected_stage,
            False,
        )
    return ScanoutFaultGateResult(
        True,
        "fault marker is bounded and correctly classified",
        requested_fault,
        expected_stage,
        False,
    )


@dataclass(frozen=True)
class ScanoutFaultGateConfig:
    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path
    fault: str


def run_scanout_fault_gate(config: ScanoutFaultGateConfig) -> ScanoutFaultGateResult:
    """Run one fault profile; watchdog-style recovery is part of its contract."""

    if config.fault not in FAULT_PROFILES:
        raise ValueError(f"unknown scanout fault: {config.fault}")
    profile = profile_by_name(FAULT_PROFILES[config.fault])
    with PinnedOutputDirectory.open(config.output_directory) as output:
        output.remove_entry("result.json")
        output.sync()
        output.verify_current()
        try:
            base = run_prepared(
                uboot=config.uboot,
                boot_disk=config.boot_disk,
                manifest=config.manifest,
                serial_log=output.path / "serial.log",
                marker_event=output.path / "marker-event.txt",
                result_path=output.path / "boot-result.json",
                startup_timeout=profile.validation.startup_timeout,
                command_timeout=profile.validation.command_timeout,
                boot_timeout=profile.validation.boot_timeout,
                termination_grace=5.0,
                profile=profile,
                device_set=DRM_SCANOUT_FAULT,
            )
            transcript_path = output.path / "serial.log"
            transcript = transcript_path.read_bytes()
            result = classify_fault_transcript(
                transcript,
                requested_fault=config.fault,
                expected_stage=config.fault,
            )
            recovered = bool(base.session.recovery_complete)
            result = ScanoutFaultGateResult(
                passed=result.passed and bool(base.passed) and recovered,
                reason=(
                    result.reason
                    if base.passed and recovered
                    else f"base run did not recover: {base.audit.failures or base.session.failure}"
                ),
                requested_fault=result.requested_fault,
                reported_stage=result.reported_stage,
                recovery_complete=recovered,
                serial_sha256=hashlib.sha256(transcript).hexdigest(),
            )
        except Exception as error:
            result = ScanoutFaultGateResult(
                False,
                f"gate error: {type(error).__name__}: {error}",
                config.fault,
                None,
                False,
            )
        with output.atomic_write(
            "result.json",
            (json.dumps(asdict(result), indent=2, sort_keys=True) + "\n").encode(),
        ):
            output.verify_current()
        return result


def _parse_args(arguments: Sequence[str] | None) -> ScanoutFaultGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--fault", required=True, choices=tuple(FAULT_PROFILES))
    parsed = parser.parse_args(arguments)
    return ScanoutFaultGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
        fault=parsed.fault,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    result = run_scanout_fault_gate(_parse_args(arguments))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
