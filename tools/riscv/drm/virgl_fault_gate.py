#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run one registered, recoverable virgl fault contract under QEMU."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from qemu_uboot_booti import run_prepared
from qemu_uboot_devices import DRM_VIRGL
from qemu_uboot_profiles import profile_by_name
from qemu_uboot_secure_io import PinnedOutputDirectory


FAULT_PROFILES = {
    "context": "generic-sv39-drm-virgl-fault-context-smp4",
    "submit": "generic-sv39-drm-virgl-fault-submit-smp4",
    "fence": "generic-sv39-drm-virgl-fault-fence-smp4",
}
FAIL_PATTERN = re.compile(rb"DRM_VIRGL_FAIL stage=([A-Za-z0-9-]+) ([^\r\n]*)")
# QEMU's serial backend records CRLF while the classifier unit fixtures use
# LF. Accept both without weakening the exact one-line marker contract.
FAULT_PATTERN = re.compile(rb"^DRM_VIRGL_FAULT stage=([A-Za-z0-9-]+)\r?$", re.MULTILINE)
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class VirglFaultGateResult:
    passed: bool
    reason: str
    requested_fault: str
    reported_stage: str | None
    recovery_complete: bool
    serial_sha256: str | None = None


def classify_fault_transcript(
    transcript: bytes, *, requested_fault: str, expected_stage: str
) -> VirglFaultGateResult:
    """Require one bounded guest failure and no false success marker."""

    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("virgl fault transcript exceeds the byte cap")
    if b"Uncaught panic" in transcript or b"unexpected exception" in transcript:
        return VirglFaultGateResult(
            False, "fatal marker in fault transcript", requested_fault, None, False
        )
    failure = FAIL_PATTERN.search(transcript)
    if failure is None:
        return VirglFaultGateResult(
            False, "missing guest failure diagnosis", requested_fault, None, False
        )
    reported = failure.group(1).decode()
    if reported != expected_stage:
        return VirglFaultGateResult(
            False,
            f"guest reported {reported} instead of {expected_stage}",
            requested_fault,
            reported,
            False,
        )
    markers = FAULT_PATTERN.findall(transcript)
    if markers != [expected_stage.encode()]:
        return VirglFaultGateResult(
            False,
            f"expected one stable fault marker, got {len(markers)}",
            requested_fault,
            reported,
            False,
        )
    if b"ASTERINAS_DRM_VIRGL_R1_READY" in transcript:
        return VirglFaultGateResult(
            False, "fault run reached the success marker", requested_fault, reported, False
        )
    return VirglFaultGateResult(
        True, "fault marker is bounded and correctly classified", requested_fault, reported, False
    )


@dataclass(frozen=True)
class VirglFaultGateConfig:
    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path
    fault: str


def run_virgl_fault_gate(config: VirglFaultGateConfig) -> VirglFaultGateResult:
    """Run one fault profile; recovery is part of the registered profile."""

    if config.fault not in FAULT_PROFILES:
        raise ValueError(f"unknown virgl fault: {config.fault}")
    profile = profile_by_name(FAULT_PROFILES[config.fault])
    expected_stage = {
        "context": "context-init",
        "submit": "submit",
        "fence": "fence",
    }[config.fault]
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
                device_set=DRM_VIRGL,
            )
            transcript = (output.path / "serial.log").read_bytes()
            result = classify_fault_transcript(
                transcript,
                requested_fault=config.fault,
                expected_stage=expected_stage,
            )
            result = VirglFaultGateResult(
                passed=result.passed and base.passed and base.session.recovery_complete,
                reason=(
                    result.reason
                    if base.passed and base.session.recovery_complete
                    else f"base run did not recover: {base.audit.failures or base.session.failure}"
                ),
                requested_fault=result.requested_fault,
                reported_stage=result.reported_stage,
                recovery_complete=base.session.recovery_complete,
                serial_sha256=hashlib.sha256(transcript).hexdigest(),
            )
        except Exception as error:
            result = VirglFaultGateResult(
                False,
                f"gate error: {type(error).__name__}: {error}",
                config.fault,
                None,
                False,
            )
        with output.atomic_write(
            "result.json", (json.dumps(asdict(result), indent=2, sort_keys=True) + "\n").encode()
        ):
            output.verify_current()
        return result


def _parse_args(arguments: Sequence[str] | None) -> VirglFaultGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--fault", required=True, choices=tuple(FAULT_PROFILES))
    parsed = parser.parse_args(arguments)
    return VirglFaultGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
        fault=parsed.fault,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    result = run_virgl_fault_gate(_parse_args(arguments))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
