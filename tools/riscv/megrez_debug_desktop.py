#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bind the Debian desktop QEMU gate to one immutable Megrez debug plan."""

from __future__ import annotations

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
    ArtifactIdentity,
    DebugContractError,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_debug_simulation import (
    SimulationError,
    _load_guarded_result,
    _remove_stale,
    _validate_current_artifacts,
)

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
BOOT_TIMEOUT = 720
SIMULATION_SETUP_GRACE_SECONDS = 120.0
DEFAULT_TIMEOUT = BOOT_TIMEOUT + SIMULATION_SETUP_GRACE_SECONDS
_DEBIAN_13_RELEASE = re.compile(r"13\.[0-9]+")
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


def simulate_desktop(
    plan: DebugPlan,
    output_directory: Path,
    *,
    run_command: RunCommand = subprocess.run,
    artifact_validator: ArtifactValidator = _validate_current_artifacts,
    repository_root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> StageResult:
    """Run the M7 Baidu desktop gate against one exact schema-2 plan."""

    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise DesktopSimulationError("desktop-timeout-invalid")
    if not math.isfinite(timeout) or timeout <= 0:
        raise DesktopSimulationError("desktop-timeout-invalid")
    if timeout < BOOT_TIMEOUT + SIMULATION_SETUP_GRACE_SECONDS:
        raise DesktopSimulationError("desktop timeout must reserve image setup grace")
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

    deadline = time.monotonic() + float(timeout)
    environment = os.environ.copy()
    old_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{repository}{os.pathsep}{old_pythonpath}"
        if old_pythonpath
        else str(repository)
    )
    command = _desktop_command(identities, native_output)
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

    result = StageResult(
        schema_version=1,
        stage="desktop",
        passed=True,
        reason="desktop-pass",
        plan_sha256=plan.plan_sha256,
        evidence=(
            "native/result.json",
            "native/desktop-m7-baidu.serial.log",
            "native/desktop-m7-baidu.ppm",
            "native/desktop-m6-javascript.ppm",
            "native/desktop-m7-baidu-home.ppm",
            "native/desktop-m7-baidu-search.ppm",
        ),
    )
    result.validate()
    return result
