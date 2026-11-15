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
from qemu_uboot_devices import DRM_FIRMWARE, QemuDeviceSet, device_set_by_name
from qemu_uboot_profiles import DRM_FIRMWARE_READY_LINE
from qemu_uboot_profiles import GENERIC_SV39_DRM_FIRMWARE_SMP4
from qemu_uboot_profiles import QemuUbootProfile, profile_by_name
from qemu_uboot_secure_io import PinnedOutputDirectory, PinnedRegularInput


DRIVER_MARKER = b"DRM_FIRMWARE_DRIVER PASS"
MAX_MARKER = b"DRM_FIRMWARE_MAX PASS"
SETCRTC_MARKER = b"DRM_FIRMWARE_SETCRTC PASS"
PAGEFLIP_MARKER = b"DRM_FIRMWARE_PAGEFLIP PASS"
DIRTYFB_MARKER = b"DRM_FIRMWARE_DIRTYFB PASS"
READY_MARKER = DRM_FIRMWARE_READY_LINE
# A failure is either an errno from a syscall or a named condition the probe
# decided for itself, so both forms have to be matched: a probe that exits with
# `detail=rect-not-copied` would otherwise read as a bare missing marker.
FAIL_PATTERN = re.compile(
    rb"DRM_FIRMWARE_FAIL stage=([A-Za-z0-9-]+) (?:errno=(-?\d+)|detail=([A-Za-z0-9-]+))"
)
MISMATCH_PATTERN = re.compile(
    rb"DRM_FIRMWARE_MISMATCH x=(\d+) y=(\d+) found=([0-9a-fA-F]+) want=([0-9a-fA-F]+) "
    rb"stage=([A-Za-z0-9-]+) where=([A-Za-z0-9-]+)"
)
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
FATAL_MARKERS = (
    b"Uncaught panic",
    b"unexpected exception",
)

# The order is the claim. Each stage depends on the one before it: the driver
# name decides which backend everything else is testing, the maximum decides
# that the mode this probe sets is one the display admits, and the three
# present paths are three different ioctls that must each put the right pixels
# on the screen. A transcript with these markers in another order is not a pass
# with a reordered log; it is a different sequence.
#
# The three are all present because covering only `SETCRTC` would leave
# `PAGE_FLIP` -- which the X server uses for every frame after the first -- and
# `DIRTYFB` to be discovered broken by a client rather than by this gate.
STAGE_SEQUENCE = (
    ("firmware driver name", re.compile(re.escape(DRIVER_MARKER))),
    ("fixed maximum mode", re.compile(re.escape(MAX_MARKER))),
    ("setcrtc presents a frame", re.compile(re.escape(SETCRTC_MARKER))),
    ("page flip presents a frame", re.compile(re.escape(PAGEFLIP_MARKER))),
    ("dirtyfb copies only the damage", re.compile(re.escape(DIRTYFB_MARKER))),
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
    """Immutable inputs and evidence directory for one firmware-gate run.

    The machine and the device set are inputs rather than constants because the
    claim this gate makes -- "the firmware backend presents frames" -- is a
    claim about a *machine*. It was proven on `qemu-virt` with Sv39; the board
    it is being brought up for runs Sv48 with `svpbmt` and `zkr` absent, and a
    path that works on one machine contract and not the other would otherwise
    be discovered on hardware. Defaults are the generic Sv39 contract, so a run
    that names neither gets exactly the machine this gate has always used.
    """

    uboot: Path
    boot_disk: Path
    manifest: Path
    output_directory: Path
    profile: QemuUbootProfile = GENERIC_SV39_DRM_FIRMWARE_SMP4
    device_set: QemuDeviceSet = DRM_FIRMWARE
    #: The generated-DTB audit written by `prepare`. The runner demands it for
    #: every machine contract that is not `VIRTUAL_PLATFORM` -- a contract
    #: approximation has to show its device tree really carries the properties
    #: it claims, which a virtual platform's does by construction. The generic
    #: Sv39 profile is a virtual platform, so this gate ran for a long time
    #: without ever needing one; the board's contract is an approximation, and
    #: without the audit the run is refused before QEMU starts.
    dtb_audit: Path | None = None


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
    # Reported before the generic checks so the coordinates and the side of the
    # damage they fell on survive -- "outside" and "inside" failing mean
    # opposite things here, and neither is visible in a bare marker count.
    mismatch = MISMATCH_PATTERN.search(transcript)
    if mismatch is not None:
        x, y, found, want, stage, where = (
            group.decode() for group in mismatch.groups()
        )
        return FirmwareGateResult(
            False,
            f"{stage}: pixel ({x},{y}) {where} the damage is 0x{found}, "
            f"should be 0x{want}",
            0,
            stage,
        )

    # The probe diagnoses its own failure; prefer that over a generic "missing
    # marker", which would not say which stage broke. It reports either an
    # errno from a syscall or a condition it decided for itself.
    failure = FAIL_PATTERN.search(transcript)
    if failure is not None:
        stage = failure.group(1).decode()
        errno_value = failure.group(2)
        detail = failure.group(3)
        if errno_value is not None:
            reason = f"guest reported {stage} failure: errno {int(errno_value)}"
        else:
            reason = f"guest reported {stage} failure: {detail.decode()}"
        return FirmwareGateResult(False, reason, 0, stage)

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
    """Run the configured firmware profile and publish final evidence."""

    profile = config.profile
    device_set = config.device_set
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
                startup_timeout=profile.validation.startup_timeout,
                command_timeout=profile.validation.command_timeout,
                boot_timeout=profile.validation.boot_timeout,
                termination_grace=5.0,
                profile=profile,
                device_set=device_set,
                dtb_audit=config.dtb_audit,
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


def _resolved(
    resolver: Callable[[str], Any], value: str, kind: str
) -> Any:
    """Resolve a registered name, turning an unknown one into a usage error.

    Both resolvers raise `ValueError`, which argparse does not catch: without
    this a typo'd profile prints a traceback and exits 1, which a caller that
    checks the exit code reads as a gate failure rather than as never having
    launched.
    """

    try:
        return resolver(value)
    except ValueError as error:
        raise SystemExit(f"error: unknown {kind}: {value}") from error


def _parse_args(arguments: Sequence[str] | None) -> FirmwareGateConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uboot", required=True, type=Path)
    parser.add_argument("--boot-disk", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument(
        "--profile",
        default=GENERIC_SV39_DRM_FIRMWARE_SMP4.name,
        help="registered QEMU U-Boot profile naming the machine contract",
    )
    parser.add_argument(
        "--device-set",
        default=DRM_FIRMWARE.name,
        help="registered device set; the run is refused unless it has a "
        "capturable display and no GPU",
    )
    parser.add_argument(
        "--dtb-audit",
        type=Path,
        default=None,
        help="generated-DTB audit from `prepare`; required by any profile "
        "whose fidelity is not VIRTUAL_PLATFORM",
    )
    parsed = parser.parse_args(arguments)
    return FirmwareGateConfig(
        uboot=parsed.uboot,
        boot_disk=parsed.boot_disk,
        manifest=parsed.manifest,
        output_directory=parsed.output_directory,
        profile=_resolved(profile_by_name, parsed.profile, "profile"),
        device_set=_resolved(device_set_by_name, parsed.device_set, "device set"),
        dtb_audit=parsed.dtb_audit,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    result = run_firmware_gate(_parse_args(arguments))
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
