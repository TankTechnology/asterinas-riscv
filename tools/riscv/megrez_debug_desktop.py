#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bind the Debian desktop QEMU gate to one immutable Megrez debug plan."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.riscv.debian.rootfs.desktop_m6_browser_gate import (
    DESKTOP_M6_JAVASCRIPT_STATUSES,
)
from tools.riscv.debian.rootfs.desktop_m7_baidu_gate import (
    classify_desktop_m7_baidu,
)
from tools.riscv.debian.rootfs.gate_protocol import GENERIC_SV39_CPU
from tools.riscv.megrez_debug_contract import (
    BROWSER_ROOT_IMAGE_BYTES,
    ArtifactIdentity,
    DebugContractError,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_physical_graphics import (
    HostGateError,
    PointerEvidenceMode,
    classify_interaction_hash_transcript,
)
from tools.riscv.megrez_debug_simulation import (
    SimulationError,
    _load_guarded_result,
    _remove_stale,
    _validate_current_artifacts,
)

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
BOOT_TIMEOUT = 720
PHYSICAL_GRAPHICS_BOOT_TIMEOUT = 1800
PHYSICAL_GRAPHICS_COMMAND_TIMEOUT = 300
SIMULATION_SETUP_GRACE_SECONDS = 120.0
DEFAULT_TIMEOUT = PHYSICAL_GRAPHICS_BOOT_TIMEOUT + SIMULATION_SETUP_GRACE_SECONDS
_DEBIAN_13_RELEASE = re.compile(r"13\.[0-9]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXPECTED_INPUT_HASHES = {
    "dtb": "qemu_dtb",
    "kernel": "kernel",
    "manifest": "root_manifest",
    "package_checksums": "package_checksums",
    "packages_lock": "packages_lock",
    "root_image": "root_image",
    "stage1_initramfs": "initramfs",
    "u_boot": "u_boot",
}
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
ArtifactValidator = Callable[[DebugPlan], dict[str, ArtifactIdentity]]


class DesktopSimulationError(RuntimeError):
    """One fail-closed desktop simulation error."""


def _safe_output(path: Path, *, repository_root: Path) -> tuple[Path, Path]:
    repository = repository_root.absolute()
    target = repository / "target"
    candidate = path.absolute()
    try:
        candidate.relative_to(target)
    except ValueError as error:
        raise DesktopSimulationError("desktop-output-outside-target") from error

    current = repository
    for component in candidate.relative_to(repository).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise DesktopSimulationError("desktop-output-unsafe")
    candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = candidate.lstat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise DesktopSimulationError("desktop-output-must-be-owned-and-mode-0700")

    native = candidate / "native"
    try:
        native.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = native.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DesktopSimulationError("desktop-native-output-unsafe")
    return candidate, native


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DesktopSimulationError("desktop-qemu-timeout")
    return remaining


def _read_evidence(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DesktopSimulationError(
            f"desktop-evidence-missing: {path.name}: {error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= MAX_EVIDENCE_BYTES
        ):
            raise DesktopSimulationError(f"desktop-evidence-invalid: {path.name}")
        payload = bytearray()
        while len(payload) <= MAX_EVIDENCE_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_EVIDENCE_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != metadata.st_size:
            raise DesktopSimulationError(f"desktop-evidence-size-changed: {path.name}")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _validate_hashes(
    native: dict[str, Any], identities: dict[str, ArtifactIdentity]
) -> None:
    recorded = native.get("input_sha256")
    expected = {
        result_name: identities[plan_name].sha256
        for result_name, plan_name in _EXPECTED_INPUT_HASHES.items()
    }
    if recorded != expected:
        raise DesktopSimulationError("desktop-native-input-hash-drift")


def _argument_value(arguments: list[str], option: str) -> str | None:
    try:
        position = arguments.index(option)
    except ValueError:
        return None
    values = arguments[position + 1 : position + 2]
    return values[0] if values else None


def _validate_qemu_arguments(native: dict[str, Any]) -> None:
    attempts = native.get("qemu_argv")
    if not isinstance(attempts, list) or not attempts:
        raise DesktopSimulationError("desktop-native-qemu-contract-drift")
    for attempt in attempts:
        if not isinstance(attempt, list) or not all(
            isinstance(argument, str) for argument in attempt
        ):
            raise DesktopSimulationError("desktop-native-qemu-contract-drift")
        if (
            _argument_value(attempt, "-cpu") != GENERIC_SV39_CPU
            or _argument_value(attempt, "-m") != "2G"
            or _argument_value(attempt, "-smp") != "4"
            or _argument_value(attempt, "-display") != "none"
            or _argument_value(attempt, "-netdev") != "user,id=net0"
            or attempt.count("virtio-net-device,netdev=net0") != 1
            or attempt.count("bochs-display") != 1
            or attempt.count("virtio-keyboard-device") != 1
            or attempt.count("virtio-tablet-device") != 1
            or sum(
                argument.startswith("virtio-blk-device,drive=") for argument in attempt
            )
            != 2
            or "-enable-kvm" in attempt
            or "-accel" in attempt
        ):
            raise DesktopSimulationError("desktop-native-qemu-contract-drift")


def _validate_screenshot(value: object, *, label: str) -> None:
    if not isinstance(value, dict):
        raise DesktopSimulationError(f"desktop-{label}-invalid")
    required = {
        "width",
        "height",
        "pixel_count",
        "distinct_sampled_colors",
        "non_background_pixels",
    }
    if set(value) != required or any(type(value[name]) is not int for name in required):
        raise DesktopSimulationError(f"desktop-{label}-invalid")
    width = value["width"]
    height = value["height"]
    pixels = value["pixel_count"]
    if (
        width < 1024
        or height < 768
        or pixels != width * height
        or value["distinct_sampled_colors"] < 64
        or value["non_background_pixels"] <= pixels // 4
    ):
        raise DesktopSimulationError(f"desktop-{label}-invalid")


def _validate_native_result(
    native: dict[str, Any], identities: dict[str, ArtifactIdentity]
) -> str:
    release = native.get("debian_release")
    javascript = native.get("javascript_status")
    if (
        native.get("passed") is not True
        or native.get("reason") != "pass"
        or native.get("profile") != "desktop-m5-network"
        or not isinstance(release, str)
        or _DEBIAN_13_RELEASE.fullmatch(release) is None
        or native.get("remote_evidence") is not True
        or javascript not in DESKTOP_M6_JAVASCRIPT_STATUSES
    ):
        raise DesktopSimulationError("desktop-native-result-invalid")
    _validate_hashes(native, identities)
    _validate_qemu_arguments(native)
    _validate_screenshot(native.get("screenshot"), label="screenshot")
    _validate_screenshot(
        native.get("javascript_screenshot"), label="javascript-screenshot"
    )
    _validate_screenshot(native.get("homepage_screenshot"), label="homepage-screenshot")
    _validate_screenshot(native.get("search_screenshot"), label="search-screenshot")
    if native.get("failure_screenshot") != {}:
        raise DesktopSimulationError("desktop-failure-screenshot-present")
    return release


def _desktop_command(
    identities: dict[str, ArtifactIdentity], native_output: Path
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "tools.riscv.debian.rootfs.desktop_m7_baidu_gate",
        "--kernel",
        identities["kernel"].path,
        "--uboot",
        identities["u_boot"].path,
        "--dtb",
        identities["qemu_dtb"].path,
        "--stage1-initramfs",
        identities["initramfs"].path,
        "--root-image",
        identities["root_image"].path,
        "--root-manifest",
        identities["root_manifest"].path,
        "--packages-lock",
        identities["packages_lock"].path,
        "--package-checksums",
        identities["package_checksums"].path,
        "--output-directory",
        str(native_output),
        "--smp",
        "4",
        "--boot-timeout",
        str(BOOT_TIMEOUT),
    ]


def _physical_graphics_command(
    identities: dict[str, ArtifactIdentity], native_output: Path
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "tools.riscv.physical_graphics_qemu_gate",
        "--kernel",
        identities["kernel"].path,
        "--uboot",
        identities["u_boot"].path,
        "--dtb",
        identities["qemu_dtb"].path,
        "--stage1-initramfs",
        identities["initramfs"].path,
        "--root-image",
        identities["root_image"].path,
        "--root-manifest",
        identities["root_manifest"].path,
        "--packages-lock",
        identities["packages_lock"].path,
        "--package-checksums",
        identities["package_checksums"].path,
        "--output-directory",
        str(native_output),
        "--smp",
        "4",
        "--boot-timeout",
        str(PHYSICAL_GRAPHICS_BOOT_TIMEOUT),
        "--command-timeout",
        str(PHYSICAL_GRAPHICS_COMMAND_TIMEOUT),
    ]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_physical_graphics_result(
    native: dict[str, Any],
    identities: dict[str, ArtifactIdentity],
    native_output: Path,
) -> tuple[str, tuple[str, ...]]:
    release = native.get("debian_release")
    if (
        native.get("passed") is not True
        or native.get("reason") != "pass"
        or native.get("profile") != "browser-web"
        or native.get("physical") is not False
        or native.get("target") != "browser"
        or native.get("screenshot") != {}
        or not isinstance(release, str)
        or _DEBIAN_13_RELEASE.fullmatch(release) is None
        or not isinstance(native.get("final_root_sha256"), str)
        or _SHA256.fullmatch(native["final_root_sha256"]) is None
        or native.get("debug_console")
        != {
            "desktop_state": "active",
            "graphical_state": "active",
            "pid1": "systemd",
            "root_device": "/dev/vdb",
            "root_filesystem": "ext2",
            "uid": 0,
        }
    ):
        raise DesktopSimulationError("desktop-native-result-invalid")
    _validate_hashes(native, identities)
    _validate_qemu_arguments(native)

    recorded_cycles = native.get("interaction_cycles")
    if not isinstance(recorded_cycles, list) or len(recorded_cycles) != 3:
        raise DesktopSimulationError("desktop-native-cycle-evidence-invalid")
    nonce_hashes = tuple(
        cycle.get("nonce_sha256") if isinstance(cycle, dict) else None
        for cycle in recorded_cycles
    )
    transcript_name = "physical-graphics-qemu.serial.log"
    transcript = _read_evidence(native_output / transcript_name)
    try:
        cycles = classify_interaction_hash_transcript(
            transcript,
            nonce_hashes,  # type: ignore[arg-type]
            pointer_mode=PointerEvidenceMode.QEMU_TABLET,
        )
    except HostGateError as error:
        raise DesktopSimulationError(f"desktop-evidence-invalid: {error}") from error
    expected_cycles = tuple(
        {
            "cycle": cycle.cycle,
            "nonce_sha256": cycle.nonce_sha256,
            "key_downs": cycle.key_downs,
            "relative_events": cycle.relative_events,
            "absolute_events": cycle.absolute_events,
            "left_down": cycle.left_down,
            "left_up": cycle.left_up,
            "evdev_sha256": cycle.evdev_sha256,
            "screenshot_sha256": cycle.screenshot_sha256,
        }
        for cycle in cycles
    )
    if tuple(recorded_cycles) != expected_cycles:
        raise DesktopSimulationError("desktop-native-cycle-evidence-invalid")
    final_marker = (
        f"__ASTERINAS_PHYSICAL_FINAL__ cycle=3 nonce_sha256={cycles[-1].nonce_sha256}"
    )
    if (
        tuple(line.rstrip("\r") for line in transcript.decode().splitlines()).count(
            final_marker
        )
        != 1
    ):
        raise DesktopSimulationError("desktop-native-final-identity-invalid")

    recorded_artifacts = native.get("cycle_artifacts")
    if not isinstance(recorded_artifacts, list) or len(recorded_artifacts) != 3:
        raise DesktopSimulationError("desktop-native-cycle-artifacts-invalid")
    evidence = ["native/result.json", f"native/{transcript_name}"]
    rendered_hashes: set[str] = set()
    for cycle, interaction, artifact in zip(
        range(1, 4), cycles, recorded_artifacts, strict=True
    ):
        if not isinstance(artifact, dict) or set(artifact) != {
            "cycle",
            "nonce_sha256",
            "guest_png_sha256",
            "rendered_ppm_sha256",
            "rendered",
        }:
            raise DesktopSimulationError("desktop-native-cycle-artifacts-invalid")
        rendered_sha256 = artifact["rendered_ppm_sha256"]
        if (
            artifact["cycle"] != cycle
            or artifact["nonce_sha256"] != interaction.nonce_sha256
            or artifact["guest_png_sha256"] != interaction.screenshot_sha256
            or not isinstance(rendered_sha256, str)
            or _SHA256.fullmatch(rendered_sha256) is None
        ):
            raise DesktopSimulationError("desktop-native-cycle-artifacts-invalid")
        _validate_screenshot(artifact["rendered"], label=f"cycle-{cycle}-rendered")
        png_name = f"physical-graphics-qemu-cycle-{cycle}.png"
        ppm_name = f"physical-graphics-qemu-cycle-{cycle}.ppm"
        if (
            _sha256(_read_evidence(native_output / png_name))
            != interaction.screenshot_sha256
            or _sha256(_read_evidence(native_output / ppm_name)) != rendered_sha256
        ):
            raise DesktopSimulationError("desktop-native-cycle-artifact-hash-drift")
        rendered_hashes.add(rendered_sha256)
        evidence.extend((f"native/{png_name}", f"native/{ppm_name}"))
    if len(rendered_hashes) != 3:
        raise DesktopSimulationError("desktop-native-rendered-artifacts-not-distinct")
    return release, tuple(evidence)


def simulate_desktop(
    plan: DebugPlan,
    output_directory: Path,
    *,
    run_command: RunCommand = subprocess.run,
    artifact_validator: ArtifactValidator = _validate_current_artifacts,
    repository_root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> StageResult:
    """Run the plan-selected graphical gate against one exact schema-2 plan."""

    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise DesktopSimulationError("desktop-timeout-invalid")
    if not math.isfinite(timeout) or timeout <= 0:
        raise DesktopSimulationError("desktop-timeout-invalid")
    if plan.schema_version != 2 or plan.profile != "debian-browser":
        raise DesktopSimulationError("desktop-plan-profile-invalid")
    try:
        plan.validate()
    except DebugContractError as error:
        raise DesktopSimulationError(f"desktop-plan-invalid: {error}") from error

    repository = (
        repository_root.absolute()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    output, native_output = _safe_output(output_directory, repository_root=repository)
    try:
        _remove_stale(output / "result.json")
        _remove_stale(native_output / "result.json")
        identities = artifact_validator(plan)
    except (DebugContractError, OSError, SimulationError) as error:
        raise DesktopSimulationError(str(error)) from error

    browser_web = identities["root_image"].size == BROWSER_ROOT_IMAGE_BYTES
    boot_timeout = PHYSICAL_GRAPHICS_BOOT_TIMEOUT if browser_web else BOOT_TIMEOUT
    if timeout < boot_timeout + SIMULATION_SETUP_GRACE_SECONDS:
        raise DesktopSimulationError("desktop timeout must reserve image setup grace")
    deadline = time.monotonic() + float(timeout)
    environment = os.environ.copy()
    old_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{repository}{os.pathsep}{old_pythonpath}"
        if old_pythonpath
        else str(repository)
    )
    command = (
        _physical_graphics_command(identities, native_output)
        if browser_web
        else _desktop_command(identities, native_output)
    )
    try:
        execution = run_command(
            command,
            cwd=repository,
            env=environment,
            check=False,
            capture_output=False,
            text=True,
            timeout=_remaining(deadline),
        )
    except subprocess.TimeoutExpired as error:
        raise DesktopSimulationError("desktop-qemu-timeout") from error
    except OSError as error:
        raise DesktopSimulationError(f"desktop-qemu-launch: {error}") from error
    if execution.returncode != 0:
        raise DesktopSimulationError(
            f"desktop-qemu-failed: exit {execution.returncode}"
        )

    try:
        native = _load_guarded_result(native_output / "result.json")
    except SimulationError as error:
        raise DesktopSimulationError(str(error)) from error
    if browser_web:
        _release, evidence = _validate_physical_graphics_result(
            native, identities, native_output
        )
    else:
        release = _validate_native_result(native, identities)
        transcript = _read_evidence(native_output / "desktop-m7-baidu.serial.log")
        classification = classify_desktop_m7_baidu(
            transcript, expected_debian_release=release
        )
        if not classification.passed:
            raise DesktopSimulationError(
                f"desktop-evidence-invalid: {classification.reason}"
            )
        _read_evidence(native_output / "desktop-m7-baidu.ppm")
        _read_evidence(native_output / "desktop-m6-javascript.ppm")
        _read_evidence(native_output / "desktop-m7-baidu-home.ppm")
        _read_evidence(native_output / "desktop-m7-baidu-search.ppm")
        evidence = (
            "native/result.json",
            "native/desktop-m7-baidu.serial.log",
            "native/desktop-m7-baidu.ppm",
            "native/desktop-m6-javascript.ppm",
            "native/desktop-m7-baidu-home.ppm",
            "native/desktop-m7-baidu-search.ppm",
        )

    result = StageResult(
        schema_version=1,
        stage="desktop",
        passed=True,
        reason="desktop-pass",
        plan_sha256=plan.plan_sha256,
        evidence=evidence,
    )
    result.validate()
    return result
