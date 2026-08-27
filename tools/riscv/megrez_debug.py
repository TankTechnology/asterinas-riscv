# SPDX-License-Identifier: MPL-2.0

"""Create and validate one simulation-first Megrez debug plan."""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from tools.riscv.megrez_debug_contract import (
    ArtifactIdentity,
    DebugContractError,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_debug_board import (
    BoardRunFailure,
    BoardTermination,
    run_physical_board,
)
from tools.riscv.megrez_debug_simulation import SimulationError, simulate_fast

KERNEL_ADDRESS = 0x80200000
INITRAMFS_ADDRESS = 0x83000000
DTB_ADDRESS = 0xF0000000
MAX_PLAN_BYTES = 1024 * 1024


class WorkflowError(RuntimeError):
    """One stable workflow failure that must not touch the board."""


def _board_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not 0 < timeout <= 300 or not math.isfinite(timeout):
        raise argparse.ArgumentTypeError("timeout must be finite and in (0, 300]")
    return timeout


def _read_regular(path: Path, *, label: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise WorkflowError(f"{label}-missing: {error}") from error
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= MAX_PLAN_BYTES:
            raise WorkflowError(f"{label}-invalid: expected a bounded regular file")
        data = bytearray()
        while len(data) <= MAX_PLAN_BYTES:
            chunk = os.read(fd, min(64 * 1024, MAX_PLAN_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != opened.st_size:
            raise WorkflowError(f"{label}-invalid: file size changed while reading")
        return bytes(data)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise WorkflowError("plan-output-unsafe")
    parent = path.parent
    if parent.exists():
        parent_info = parent.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise WorkflowError("plan-output-parent-unsafe")
    else:
        parent.mkdir(parents=True, mode=0o755)

    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(temporary_fd, 0o644)
        view = memoryview(payload)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise WorkflowError("plan-output-short-write")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_plan(path: Path) -> DebugPlan:
    try:
        return DebugPlan.from_bytes(_read_regular(path, label="plan"))
    except DebugContractError as error:
        raise WorkflowError(f"plan-invalid: {error}") from error


def _check_artifacts(plan: DebugPlan) -> None:
    for expected in plan.artifacts:
        try:
            actual = ArtifactIdentity.from_path(
                expected.name, Path(expected.path), expected.load_address
            )
        except (DebugContractError, OSError) as error:
            raise WorkflowError(
                f"plan-artifact-drift: {expected.name}: {error}"
            ) from error
        if actual != expected:
            raise WorkflowError(f"plan-artifact-drift: {expected.name}")


def _create_plan(arguments: argparse.Namespace) -> DebugPlan:
    artifacts = (
        ArtifactIdentity.from_path("kernel", arguments.kernel, KERNEL_ADDRESS),
        ArtifactIdentity.from_path("initramfs", arguments.initramfs, INITRAMFS_ADDRESS),
        ArtifactIdentity.from_path("qemu_dtb", arguments.qemu_dtb, DTB_ADDRESS),
        ArtifactIdentity.from_path("megrez_dtb", arguments.megrez_dtb, DTB_ADDRESS),
    )
    plan = DebugPlan(
        schema_version=1,
        profile="tcp-probe",
        artifacts=artifacts,
        bootargs=arguments.bootargs,
        smp=4,
        sv39=True,
        markers=tuple(arguments.marker),
        reboot_after=arguments.reboot_after,
    )
    plan.validate()
    return plan


def _physical_actions(plan: DebugPlan) -> list[dict[str, object]]:
    identities = {identity.name: identity for identity in plan.artifacts}
    actions: list[dict[str, object]] = [
        {"action": "require-simulation", "tier": "fast"},
        {"action": "probe-uboot-baud", "choices": [115200, 1500000]},
    ]
    for name in ("kernel", "initramfs", "megrez_dtb"):
        identity = identities[name]
        actions.append(
            {
                "action": "cache-or-transfer",
                "artifact": name,
                "address": identity.load_address,
            }
        )
    actions.extend(
        (
            {"action": "boot-once", "reboot_after": plan.reboot_after},
            {"action": "capture-markers"},
            {"action": "await-automatic-recovery"},
        )
    )
    return actions


def _validate_simulation(path: Path, plan: DebugPlan) -> None:
    try:
        result = StageResult.from_bytes(_read_regular(path, label="plan-simulation"))
    except DebugContractError as error:
        raise WorkflowError(f"plan-simulation-invalid: {error}") from error
    if (
        result.stage != "fast"
        or not result.passed
        or result.plan_sha256 != plan.plan_sha256
    ):
        raise WorkflowError("plan-simulation-mismatch")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="freeze one exact artifact set")
    plan.add_argument("--kernel", required=True, type=Path)
    plan.add_argument("--initramfs", required=True, type=Path)
    plan.add_argument("--qemu-dtb", required=True, type=Path)
    plan.add_argument("--megrez-dtb", required=True, type=Path)
    plan.add_argument("--bootargs", required=True)
    plan.add_argument("--marker", action="append", required=True)
    plan.add_argument("--reboot-after", type=int, default=180)
    plan.add_argument("--output", required=True, type=Path)

    check = subparsers.add_parser("check", help="revalidate every plan artifact")
    check.add_argument("plan", type=Path)

    simulate = subparsers.add_parser("simulate", help="run a plan-bound QEMU gate")
    simulate.add_argument("plan", type=Path)
    simulate.add_argument("--tier", choices=("fast",), required=True)
    simulate.add_argument("--output-directory", required=True, type=Path)
    simulate.add_argument("--uboot-build-directory", required=True, type=Path)

    board = subparsers.add_parser("board", help="show or execute physical actions")
    board.add_argument("plan", type=Path)
    board.add_argument("device")
    board.add_argument("--simulation-result", required=True, type=Path)
    board.add_argument("--output-directory", type=Path)
    board.add_argument("--timeout", type=_board_timeout, default=300.0)
    board.add_argument("--dry-run", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        values = _parser().parse_args(arguments)
        if values.command == "plan":
            plan = _create_plan(values)
            _atomic_write(values.output, plan.canonical_bytes())
            return 0
        if values.command == "check":
            plan = _load_plan(values.plan)
            _check_artifacts(plan)
            print(f"MEGREZ_DEBUG_CHECK_PASS plan={plan.plan_sha256}")
            return 0
        if values.command == "simulate":
            plan = _load_plan(values.plan)
            _check_artifacts(plan)
            result = simulate_fast(
                plan,
                values.output_directory,
                values.uboot_build_directory,
            )
            _atomic_write(
                values.output_directory / "result.json", result.canonical_bytes()
            )
            return 0

        plan = _load_plan(values.plan)
        if values.dry_run:
            print(json.dumps(_physical_actions(plan), separators=(",", ":")))
            return 0
        _check_artifacts(plan)
        _validate_simulation(values.simulation_result, plan)
        if values.output_directory is None:
            raise WorkflowError("board-output-directory-required")
        result = run_physical_board(
            plan,
            values.device,
            values.output_directory,
            timeout=values.timeout,
        )
        return 0 if result.passed else 2
    except BoardTermination as error:
        return 128 + error.signum
    except (
        BoardRunFailure,
        DebugContractError,
        OSError,
        SimulationError,
        WorkflowError,
    ) as error:
        print(f"megrez-debug: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
