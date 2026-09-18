#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run and classify the RISC-V DRM virgl capability gate."""

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
from qemu_uboot_devices import DRM_VIRGL, DeviceKind, QemuDeviceSet, device_set_by_name
from qemu_uboot_profiles import DRM_VIRGL_READY_LINE
from qemu_uboot_profiles import GENERIC_SV39_DRM_VIRGL_SMP4 as VIRGL_PROFILE
from qemu_uboot_secure_io import PinnedOutputDirectory, PinnedRegularInput


READY_MARKER = DRM_VIRGL_READY_LINE
PARAM_PATTERN = re.compile(rb"DRM_VIRGL_PARAM 3d=(\d+) capsets=0x([0-9a-f]+)")
CAPS_PATTERN = re.compile(rb"DRM_VIRGL_CAPS PASS caps_bytes=(\d+)")
CONTEXT_MARKER = b"DRM_VIRGL_CONTEXT PASS"
FAIL_PATTERN = re.compile(rb"DRM_VIRGL_FAIL stage=([A-Za-z0-9-]+) ([^\r\n]*)")
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
#: The capset bit a virgl host sets in `VIRTGPU_PARAM_SUPPORTED_CAPSET_IDs`.
VIRGL_CAPSET_ID = 1
#: How much of the capability blob must carry host data. Below this the buffer
#: still holds the probe's own fill pattern, so nothing was copied.
CAPS_MIN_DELIVERED = 256
FATAL_MARKERS = (
    b"Uncaught panic",
    b"unexpected exception",
)


@dataclass(frozen=True)
class VirglParamGateResult:
    passed: bool
    reason: str
    reported_3d: int
    reported_capsets: int
    expected_3d: int
    caps_bytes: int


@dataclass(frozen=True)
class VirglParamGateConfig:
    """Immutable inputs and evidence directory for one virgl-gate run."""

    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path
    device_set: QemuDeviceSet = DRM_VIRGL


def expects_3d(device_set: QemuDeviceSet) -> bool:
    """Whether the device set should give the guest a 3D-capable device.

    Derived rather than configured: the device is what decides whether QEMU
    offers the virgl feature bit, so letting a caller assert an expectation the
    device cannot meet would only produce a gate that lies. It also makes the
    control run automatic — the same probe on a plain device has to report no
    3D, or the report is not tracking the device at all.
    """

    return DeviceKind.VIRTIO_GPU_GL in device_set.devices


def classify_transcript(
    transcript: bytes, *, expected_3d: bool
) -> VirglParamGateResult:
    """Require the probe's report to match what its device should provide."""

    expected = int(expected_3d)
    if len(transcript) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("virgl transcript exceeds the byte cap")

    for marker in FATAL_MARKERS:
        if marker in transcript:
            return VirglParamGateResult(
                False, f"fatal marker: {marker.decode()}", -1, 0, expected, 0
            )

    failure = FAIL_PATTERN.search(transcript)
    if failure is not None:
        stage = failure.group(1).decode()
        trailing = failure.group(2).decode()
        return VirglParamGateResult(
            False, f"guest reported {stage} failure: {trailing}", -1, 0, expected, 0
        )

    match = PARAM_PATTERN.search(transcript)
    if match is None:
        return VirglParamGateResult(
            False, "missing virgl parameter report", -1, 0, expected, 0
        )
    reported_3d = int(match.group(1))
    capsets = int(match.group(2), 16)
    rest = transcript[match.end() :]

    caps_match = CAPS_PATTERN.search(rest)
    if caps_match is None:
        return VirglParamGateResult(
            False, "missing capability blob report", reported_3d, capsets, expected, 0
        )
    caps_bytes = int(caps_match.group(1))

    # The sequence is what proves the probe walked the whole path, so the
    # markers have to follow one another rather than merely be present.
    context_at = rest.find(CONTEXT_MARKER, caps_match.end())
    if context_at < 0:
        return VirglParamGateResult(
            False,
            "missing or unordered context marker",
            reported_3d,
            capsets,
            expected,
            caps_bytes,
        )
    if READY_MARKER not in rest[context_at + len(CONTEXT_MARKER) :]:
        return VirglParamGateResult(
            False,
            "missing ready marker after the context",
            reported_3d,
            capsets,
            expected,
            caps_bytes,
        )

    if reported_3d != expected:
        return VirglParamGateResult(
            False,
            f"reported 3D={reported_3d} but this device should report {expected}",
            reported_3d,
            capsets,
            expected,
            caps_bytes,
        )

    # A host that reports 3D must also name the renderer's capset; a feature
    # flag without a capset is a 3D claim nothing can be created against.
    if expected_3d and not capsets & (1 << VIRGL_CAPSET_ID):
        return VirglParamGateResult(
            False,
            f"3D reported but capset mask {capsets:#x} lacks virgl",
            reported_3d,
            capsets,
            expected,
            caps_bytes,
        )
    if not expected_3d and capsets != 0:
        return VirglParamGateResult(
            False,
            f"no 3D reported but capset mask {capsets:#x} is non-empty",
            reported_3d,
            capsets,
            expected,
            caps_bytes,
        )

    # The blob is the renderer's own description of itself, so a 3D host has
    # to deliver one and a host without 3D must not pretend to.
    if expected_3d and caps_bytes < CAPS_MIN_DELIVERED:
        return VirglParamGateResult(
            False,
            f"3D reported but only {caps_bytes} capability bytes were delivered",
            reported_3d,
            capsets,
            expected,
            caps_bytes,
        )
    if not expected_3d and caps_bytes != 0:
        return VirglParamGateResult(
            False,
            f"no 3D reported but {caps_bytes} capability bytes were delivered",
            reported_3d,
            capsets,
            expected,
            caps_bytes,
        )

    return VirglParamGateResult(
        True, "passed", reported_3d, capsets, expected, caps_bytes
    )


def _read_serial_log(path: Path) -> bytes:
    with (
        PinnedRegularInput.open(path, label="virgl serial log") as serial,
        tempfile.TemporaryDirectory(prefix="asterinas-drm-virgl-") as temporary,
    ):
        copy = Path(temporary) / "serial.log"
        serial.copy_to(copy)
        if copy.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise ValueError("virgl transcript exceeds the byte cap")
        return copy.read_bytes()


def _publish_result(
    output: PinnedOutputDirectory,
    result: VirglParamGateResult,
) -> None:
    document = json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
    with output.atomic_write("result.json", document.encode()):
        output.verify_current()


def run_virgl_param_gate(
    config: VirglParamGateConfig,
    *,
    runner: Callable[..., Any] = run_prepared,
) -> VirglParamGateResult:
    """Run the registered SMP=4 virgl profile and publish final evidence."""

    expected_3d = expects_3d(config.device_set)
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
                startup_timeout=VIRGL_PROFILE.validation.startup_timeout,
                command_timeout=VIRGL_PROFILE.validation.command_timeout,
                boot_timeout=VIRGL_PROFILE.validation.boot_timeout,
                termination_grace=5.0,
                profile=VIRGL_PROFILE,
                device_set=config.device_set,
            )
            classified = classify_transcript(
                _read_serial_log(output.path / "serial.log"),
                expected_3d=expected_3d,
            )
            if not bool(base_result.passed):
                result = VirglParamGateResult(
                    False,
                    "base U-Boot gate failed",
                    classified.reported_3d,
                    classified.reported_capsets,
                    expected_3d,
                    classified.caps_bytes,
                )
            else:
                result = classified
        except Exception as error:
            result = VirglParamGateResult(
                False,
                f"gate error: {type(error).__name__}: {error}",
                -1,
                0,
                int(expected_3d),
                0,
            )
        _publish_result(output, result)
        return result


def _parse_args(arguments: Sequence[str] | None) -> VirglParamGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument(
        "--device-set",
        default=DRM_VIRGL.name,
        help="registered device set to launch; a non-GL one is the control run",
    )
    parsed = parser.parse_args(arguments)
    try:
        device_set = device_set_by_name(parsed.device_set)
    except ValueError as error:
        parser.error(str(error))
    return VirglParamGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
        device_set=device_set,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    result = run_virgl_param_gate(_parse_args(arguments))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
