# SPDX-License-Identifier: MPL-2.0

"""Run one plan-bound Megrez fast simulation through the guarded QEMU gate."""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.riscv.megrez_debug_contract import (
    ArtifactIdentity,
    DebugContractError,
    DebugPlan,
    StageResult,
)

FAST_PROFILE = "generic-sv39-smp4-tcp-probe"
MAX_RUN_RESULT_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 300.0
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class SimulationError(RuntimeError):
    """One stable failure from preparation or the guarded QEMU fast gate."""


def _safe_work_directory(path: Path, *, repository_root: Path, label: str) -> Path:
    repository = repository_root.absolute()
    allowed = repository / "target" / "qemu-uboot"
    candidate = path.absolute()
    try:
        candidate.relative_to(allowed)
    except ValueError as error:
        raise SimulationError(f"{label}-outside-qemu-output-root") from error

    current = repository
    for component in candidate.relative_to(repository).parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SimulationError(f"{label}-unsafe")
    candidate.mkdir(parents=True, mode=0o755, exist_ok=True)
    return candidate


def _remove_stale(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SimulationError(f"simulation-output-unsafe: {path.name}")
    path.unlink()


def _validate_current_artifacts(plan: DebugPlan) -> dict[str, ArtifactIdentity]:
    plan.validate()
    identities = {identity.name: identity for identity in plan.artifacts}
    for expected in plan.artifacts:
        try:
            current = ArtifactIdentity.from_path(
                expected.name, Path(expected.path), expected.load_address
            )
        except (DebugContractError, OSError) as error:
            raise SimulationError(
                f"plan-artifact-drift: {expected.name}: {error}"
            ) from error
        if current != expected:
            raise SimulationError(f"plan-artifact-drift: {expected.name}")
    return identities


def _remaining(deadline: float, *, phase: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SimulationError(f"{phase}-timeout")
    return remaining


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    repository_root: Path,
    deadline: float,
    phase: str,
    run_command: RunCommand,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_command(
            command,
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=False,
            text=True,
            timeout=_remaining(deadline, phase=phase),
        )
    except subprocess.TimeoutExpired as error:
        raise SimulationError(f"{phase}-timeout") from error
    except OSError as error:
        raise SimulationError(f"{phase}-launch: {error}") from error


def _load_guarded_result(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise SimulationError(f"qemu-result-missing: {error}") from error
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size <= MAX_RUN_RESULT_BYTES
        ):
            raise SimulationError("qemu-result-invalid: expected bounded regular file")
        payload = bytearray()
        while len(payload) <= MAX_RUN_RESULT_BYTES:
            chunk = os.read(fd, min(64 * 1024, MAX_RUN_RESULT_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != info.st_size:
            raise SimulationError("qemu-result-invalid: size changed")
    finally:
        os.close(fd)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SimulationError(f"qemu-result-invalid: duplicate key {key}")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SimulationError("qemu-result-invalid: malformed JSON") from error
    if not isinstance(decoded, dict):
        raise SimulationError("qemu-result-invalid: expected object")
    return decoded


def simulate_fast(
    plan: DebugPlan,
    output_directory: Path,
    build_directory: Path,
    *,
    run_command: RunCommand = subprocess.run,
    repository_root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> StageResult:
    """Run the exact plan through preparation and the registered fast gate."""

    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise SimulationError("simulation-timeout-invalid")
    if not math.isfinite(timeout) or timeout <= 0:
        raise SimulationError("simulation-timeout-invalid")
    repository = (
        repository_root.absolute()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    output = _safe_work_directory(
        output_directory, repository_root=repository, label="simulation-output"
    )
    build = _safe_work_directory(
        build_directory, repository_root=repository, label="simulation-build"
    )
    for name in ("result.json", "qemu-result.json"):
        _remove_stale(output / name)

    identities = _validate_current_artifacts(plan)
    deadline = time.monotonic() + float(timeout)
    environment = os.environ.copy()
    environment.update(
        {
            "ASTERINAS_RISCV_BOOTI": identities["kernel"].path,
            "ASTERINAS_INITRAMFS": identities["initramfs"].path,
            "QEMU_UBOOT_PROFILE": FAST_PROFILE,
            "QEMU_UBOOT_OUT_DIR": str(output),
            "QEMU_UBOOT_BUILD_DIR": str(build),
        }
    )
    preparation = _run(
        [str(repository / "tools/riscv/prepare_qemu_uboot_booti.sh"), "prepare"],
        environment=environment,
        repository_root=repository,
        deadline=deadline,
        phase="prepare",
        run_command=run_command,
    )
    if preparation.returncode != 0:
        raise SimulationError(f"prepare-failed: exit {preparation.returncode}")

    expected_dtb = identities["qemu_dtb"]
    try:
        generated_dtb = ArtifactIdentity.from_path(
            "qemu_dtb", output / "qemu-virt.dtb", expected_dtb.load_address
        )
    except (DebugContractError, OSError) as error:
        raise SimulationError(f"qemu-dtb-drift: {error}") from error
    if generated_dtb != expected_dtb:
        raise SimulationError("qemu-dtb-drift")

    qemu_result = output / "qemu-result.json"
    execution = _run(
        [
            sys.executable,
            str(repository / "tools/riscv/qemu_uboot_booti.py"),
            "run",
            "--profile",
            FAST_PROFILE,
            "--uboot",
            str(build / "u-boot"),
            "--boot-disk",
            str(output / "boot.ext4"),
            "--manifest",
            str(output / "artifacts.json"),
            "--dtb-audit",
            str(output / "qemu-dtb-audit.json"),
            "--serial-log",
            str(output / "serial.log"),
            "--marker-event",
            str(output / "marker-event.txt"),
            "--result",
            str(qemu_result),
            "--bootargs-override",
            plan.bootargs,
        ],
        environment=environment,
        repository_root=repository,
        deadline=deadline,
        phase="qemu",
        run_command=run_command,
    )
    if execution.returncode != 0:
        raise SimulationError(f"qemu-failed: exit {execution.returncode}")
    guarded = _load_guarded_result(qemu_result)
    qemu_arguments = guarded.get("qemu_argv")
    if (
        guarded.get("passed") is not True
        or guarded.get("profile") != FAST_PROFILE
        or guarded.get("status") != "PASS"
        or guarded.get("terminal_classification") != "BOOT_COMPLETED"
        or guarded.get("effective_bootargs") != plan.bootargs
        or not isinstance(qemu_arguments, list)
        or not all(isinstance(argument, str) for argument in qemu_arguments)
        or "-smp" not in qemu_arguments
        or qemu_arguments[qemu_arguments.index("-smp") + 1 :][:1] != ["4"]
        or "-cpu" not in qemu_arguments
        or qemu_arguments[qemu_arguments.index("-cpu") + 1 :][:1]
        != ["rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true"]
    ):
        raise SimulationError("qemu-gate-failed")

    result = StageResult(
        schema_version=1,
        stage="fast",
        passed=True,
        reason="fast-pass",
        plan_sha256=plan.plan_sha256,
        evidence=(
            "serial.log",
            "marker-event.txt",
            "qemu-result.json",
            "qemu-dtb-audit.json",
        ),
    )
    result.validate()
    return result
